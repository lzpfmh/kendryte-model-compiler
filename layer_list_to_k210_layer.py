'''
 * Copyright 2018 Canaan Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 '''

import math

import tensor_list_to_layer_list
import numpy as np


def hotfix_magic_1(eight_bit_mode, scale_max):
    # 36bit before and after bn
    # bn_add range is 32bit
    return 1 / max(scale_max, 1 / (1 << 32))


def log_next_pow_of_2(value):
    ret = 0
    while value > 1 or value <= -1:
        value = value / 2
        ret = ret + 1

    return ret, value


def pow_next_log_of_2(value, bound_shift, shift_max_shift=4):
    ret = 0
    shift_max = 1 << shift_max_shift
    while value >= -(1 << (bound_shift - 2)) and value < (1 << (bound_shift - 2)) \
            and value != 0 and ret < (shift_max - 1):
        value = value * 2
        ret = ret + 1

    return ret, value


def signed_to_hex(value, width):
    return hex(int((1 << width) + value) % (1 << width))


class K210Conv:
    def __init__(self, weights, depth_wise_layer, eight_bit_mode, xy_shape, xw_minmax, tensor_info):
        self.weights = weights
        self.weights_shape = self.weights.shape
        self.input_shape, self.output_shape = xy_shape
        xmin, xmax, wmin, wmax = xw_minmax
        self.stride = 1
        self.depth_wise_layer = depth_wise_layer
        self.eight_bit_mode = eight_bit_mode

        self.x_range = xmax - xmin
        self.x_bias = xmin
        assert (self.x_range > 0)

        self.w_range = wmax - wmin
        self.w_bias = wmin
        assert (self.w_range > 0)

        if self.input_shape[1:3] != self.output_shape[1:3]:
            # raise ValueError('conv2d {} should use padding=SAME'.format(input_tensor_name))
            print('[error]', 'conv2d {} should use padding=SAME'.format(tensor_info.get('name', 'noname')))
            self.input_shape = list(self.input_shape)
            self.input_shape[1] = self.output_shape[1]
            self.input_shape[2] = self.output_shape[2]

        if self.input_shape[1] < 4:
            tensor_height = self.input_shape[1]
            print('[error] feature map required height>4 which {} height is {}' \
                  .format(tensor_info.get('name', 'noname'), tensor_height))
            self.input_shape = list(self.input_shape)
            self.output_shape = list(self.output_shape)
            old_input_wh = self.input_shape[1:3]
            old_output_wh = self.output_shape[1:3]
            self.input_shape[1:3] = [4, 4]
            self.output_shape[1:3] = [4, 4]
            notice = 'tensor {} heigh-width MUST padding from {}x{}=>{}x{} to 4x4=>4x4 in CPU before continue.' \
                .format(tensor_info.get('name', 'noname'), *old_input_wh, *old_output_wh)
            print('[notice] ' + ('=' * 71))
            print('[notice] ' + notice)
            print('[notice] ' + ('=' * 71))

    @staticmethod
    def q(value, scale, bias):
        return (value - bias) / scale

    def para_mult_loads(self, weights_shape, output_shape, kernel_size):
        weight_buffer_size = 2 * 9 * 4096
        weights_ich = int(weights_shape[2])
        weights_och = int(weights_shape[3])
        weight_data_size = 1 if self.eight_bit_mode else 2

        if self.depth_wise_layer:
            o_ch_weights_size = int(weights_shape[0]) * int(weights_shape[1]) * weight_data_size
        else:
            o_ch_weights_size = int(weights_shape[0]) * int(weights_shape[1]) * int(weights_shape[2]) * weight_data_size

        if int(weights_shape[0]) == 1:
            o_ch_weights_size_pad = math.ceil(o_ch_weights_size / 8) * 9
        else:
            o_ch_weights_size_pad = o_ch_weights_size
            assert (int(weights_shape[0]) == 3)

        if kernel_size == 3:
            load_time = math.ceil(weights_och / math.floor(4096 * 2 / weight_data_size / weights_ich))
        elif kernel_size == 1:
            load_time = math.ceil(weights_och / math.floor(4096 * 8 * 2 / weight_data_size / weights_ich))
        else:
            load_time = None
            assert (None)

        o_ch_num = int(output_shape[3])
        o_ch_num_coef = math.floor(weight_buffer_size / o_ch_weights_size_pad)

        if self.eight_bit_mode:
            half_weight_buffer_size = weight_buffer_size / 2
            while True:
                last_ch_idx = (o_ch_num - 1) % o_ch_num_coef
                last_addr_end = (last_ch_idx + 1) * o_ch_weights_size_pad
                if last_addr_end < half_weight_buffer_size:
                    break

                o_ch_num_coef = o_ch_num_coef - 1
                load_time = math.ceil(o_ch_num / o_ch_num_coef)
                if o_ch_num_coef <= 0:
                    assert ('cannot fix last_addr_end to first half part')

        assert (load_time <= 64)

        o_ch_num_coef = min(o_ch_num_coef, o_ch_num)
        para_size = o_ch_num_coef * o_ch_weights_size
        return load_time, para_size, o_ch_num_coef

    def to_k210(self):
        input_shape = self.input_shape
        output_shape = self.output_shape
        weights_shape = self.weights_shape
        weights = self.weights
        stride = self.stride

        weight_data_size = 1 if self.eight_bit_mode else 2
        kernel_size = int(weights_shape[0])

        # img i
        i_row_wid = int(input_shape[2])
        i_col_high = int(input_shape[1])
        coef_group = 1 if i_row_wid > 32 else (2 if i_row_wid > 16 else 4)
        row_switch_addr = math.ceil(i_row_wid / 64)
        channel_switch_addr = i_col_high * row_switch_addr
        # conv
        depth_wise_layer = 1 if self.depth_wise_layer else 0
        kernel_type = {1: 0, 3: 1}[kernel_size]
        pad_type = 0
        load_coor = 1

        first_stride = 0 if stride == 1 else 1
        assert (256 > (i_col_high if first_stride == 0 else i_col_high / 2))

        load_time, para_size, o_ch_num_coef = self.para_mult_loads(weights_shape, output_shape, kernel_size)

        x_qmax = 255
        w_qmax = (1 << (8 * weight_data_size)) - 1
        bias_x, scale_x = self.x_bias, self.x_range / x_qmax
        bias_w, scale_w = self.w_bias, self.w_range / w_qmax

        bx_div_sx = bias_x / scale_x
        bw_div_sw = bias_w / scale_w

        shr_x, arg_x = pow_next_log_of_2(bw_div_sw, 24)
        shr_w, arg_w = pow_next_log_of_2(bx_div_sx, 24)
        arg_add = kernel_size * kernel_size * bw_div_sw * bx_div_sx
        pad_value = -bx_div_sx
        swsx = scale_w * scale_x

        weight_q = ((weights - bias_w) / scale_w).transpose([3, 2, 0, 1])
        para_start_addr = [int(round(item)) for item in np.reshape(weight_q, (np.product(weight_q.shape),))]

        return {
            'swsx': swsx,
            'coef_group': coef_group,
            'channel_switch_addr': channel_switch_addr,
            'depth_wise_layer': depth_wise_layer,
            'o_ch_num_coef': o_ch_num_coef,
            'i_row_wid': i_row_wid,
            'i_col_high': i_col_high,
            'kernel_type': kernel_type,
            'pad_type': pad_type,
            'first_stride': first_stride,
            'pad_value': pad_value,
            'load_coor': load_coor,
            'load_time': load_time,
            'para_size': para_size,
            'para_start_addr': para_start_addr,
            'row_switch_addr': row_switch_addr,
            'shr_w': shr_w,
            'shr_x': shr_x,
            'arg_w': arg_w,
            'arg_x': arg_x,
            'arg_add': arg_add
        }


class K210BN:
    def __init__(self, mean, var, gamma, beta, epsilon, eight_bit_mode, tensor_info=None):
        self.mean = mean
        self.var = var
        self.gamma = gamma
        self.beta = beta
        self.epsilon = epsilon
        self.eight_bit_mode = eight_bit_mode
        self.tensor_info = tensor_info or dict()

    @staticmethod
    def get_bn(scale, bias):
        norm_shift, norm_mul = pow_next_log_of_2(scale, 24)
        return {'norm_mul': signed_to_hex(norm_mul, 24), 'norm_add': signed_to_hex(bias, 32), 'norm_shift': norm_shift}

    def to_k210(self, swsx=1):
        sqrt_var = np.sqrt(self.var + self.epsilon)
        post_scale = hotfix_magic_1(self.eight_bit_mode, max(swsx * self.gamma / sqrt_var))
        scale = swsx * self.gamma / sqrt_var * post_scale
        bias = (self.beta - self.gamma * self.mean / sqrt_var) * post_scale

        load_para = 1
        bwsx_base_addr = [
            self.get_bn(s, b)
            for s, b in zip(scale.tolist(), bias.tolist())
        ]

        return locals()


class K210Act:
    def __init__(self, min_y, max_y, ty, eight_bit_mode, tensor_info=None):
        self.ty = ty
        self.eight_bit_mode = eight_bit_mode
        self.min_y = min_y
        self.max_y = max_y
        self.tensor_info = tensor_info or dict()

    @staticmethod
    def leaky_relu(x):
        return x if x >= 0 else 0.1 * x

    @staticmethod
    def leaky_relu_inverse(y):
        return y if y >= 0 else 10 * y

    @staticmethod
    def relu_inverse(y):
        return y

    @staticmethod
    def relu6_inverse(y):
        return y

    @staticmethod
    def leaky_table(min_y, max_y):
        range_y = max_y - min_y
        y_table = [min_y + i * range_y / 15 for i in range(15)]
        y_table.append(max_y)
        if 0 not in y_table:
            y_table.append(0)
        y_table = sorted(y_table)
        x_table = [K210Act.leaky_relu_inverse(it) for it in y_table]
        dydx = [(y_table[i + 1] - y_table[i]) / (x_table[i + 1] - x_table[i]) for i in range(len(y_table) - 1)]
        return zip(x_table, y_table, dydx)

    @staticmethod
    def relu_table(min_y, max_y):
        range_y = max_y - min_y
        y_table = [min_y + i * range_y / 15 for i in range(15)]
        y_table.append(max_y)
        if 0 not in y_table:
            y_table.append(0)
        y_table = sorted(y_table)
        x_table = [K210Act.relu_inverse(it) for it in y_table]
        dydx = [(y_table[i + 1] - y_table[i]) / (x_table[i + 1] - x_table[i]) for i in range(len(y_table) - 1)]
        return zip(x_table, y_table, dydx)

    @staticmethod
    def relu6_table(min_y, max_y):
        range_y = max_y - min_y
        y_table = [min_y + i * range_y / 15 for i in range(15)]
        y_table.append(max_y)
        if 0 not in y_table:
            y_table.append(0)
        y_table = sorted(y_table)
        x_table = [K210Act.relu6_inverse(it) for it in y_table]
        dydx = [(y_table[i + 1] - y_table[i]) / (x_table[i + 1] - x_table[i]) for i in range(len(y_table) - 1)]
        return zip(x_table, y_table, dydx)

    @staticmethod
    def linear_table(min_y, max_y):
        range_y = max_y - min_y
        y_table = [min_y + i * range_y / 14 for i in range(14)]
        if 0 not in y_table:
            y_table.append(0)
        y_table.append(max_y)
        y_table = sorted(y_table)
        return zip(y_table, y_table, [1] * (len(y_table) - 1))

    @staticmethod
    def find_shift(dydx):
        ret_shift = 0
        while abs(dydx) < (1 << 14) and dydx > 0:
            dydx = dydx * 2
            ret_shift = ret_shift + 1
        return ret_shift, dydx

    @staticmethod
    def table_to_act(act_table, min_y, max_y, eight_bit_mode, post_scale):
        def act_table_aux(x, y, dydx):
            y_scale = (max_y - min_y) / 255
            y_bias = min_y
            x_fix = x * post_scale
            y_fix = (y - y_bias) / y_scale
            dydx_fix = dydx / y_scale / post_scale

            yf_q = round(y_fix)
            yf_err = y_fix - yf_q
            xfy = x_fix - yf_err / dydx_fix
            return xfy, yf_q, dydx_fix

        act_table = [(0x800000000, 0, 0)] + [act_table_aux(x, y, dydx) for x, y, dydx in act_table]

        def ret_aux(x, y, dydx):
            dxss, dys = K210Act.find_shift(dydx)
            assert (dys >= 0)
            return {'x': int(round(x)), 'y': int(round(y)), 'dxs': dxss, 'dy': int(round(dys))}

        return [ret_aux(x, y, dydx) for x, y, dydx in act_table]

    def to_k210(self, post_scale):
        act_tab = None
        if self.ty == 'leaky':
            act_tab = list(K210Act.leaky_table(self.min_y, self.max_y))
        elif self.ty == 'Relu':
            act_tab = list(K210Act.relu_table(self.min_y, self.max_y))
        elif self.ty == 'Relu6':
            act_tab = list(K210Act.relu6_table(self.min_y, self.max_y))
        elif self.ty == 'linear':
            act_tab = list(K210Act.linear_table(self.min_y, self.max_y))
        else:
            assert ValueError(self.ty, ' active is not supported.')

        active_tab = K210Act.table_to_act(list(act_tab), self.min_y, self.max_y, self.eight_bit_mode, post_scale)
        return {'active_addr': active_tab[:16]}


class K210Pool:
    def __init__(self, pool_type, size, stride, tensor_info=None):
        self.size = size
        self.stride = stride
        self.pool_type = pool_type
        self.tensor_info = tensor_info or dict()

    def to_k210(self):
        if self.pool_type == 'MaxPool':
            return {'pool_type': {
                (2, 2): 1,
                (4, 4): 3,
                (2, 1): 9
            }[(self.size, self.stride)]}
        elif self.pool_type == 'AvgPool':
            return {'pool_type': {
                (2, 2): 2,
                (4, 4): 4,
                (2, 1): 8
            }[(self.size, self.stride)]}
        elif self.pool_type == 'hotfix_leftPool':
            return {'pool_type': {
                (2, 2): 5,
                (4, 4): 7,
            }[(self.size, self.stride)]}
        elif self.pool_type == 'hotfix_rightPool':
            return {'pool_type': 6}
        else:
            return None


class K210Layer:
    def __init__(self, eight_bit_mode):
        self.conv = None
        self.bn = None
        self.act = None
        self.pool = None
        self.eight_bit_mode = eight_bit_mode

    @staticmethod
    def batch(iter, n=1):
        l = len(iter)
        for ndx in range(0, l, n):
            yield iter[ndx:min(ndx + n, l)]

    def to_k210(self):
        if self.pool is not None:
            output_shape = list(self.conv.output_shape)
            output_shape[1] = int(math.floor(self.conv.output_shape[1] / self.pool.stride))
            output_shape[2] = int(math.floor(self.conv.output_shape[2] / self.pool.stride))
        else:
            output_shape = self.conv.output_shape

        weights_shape = self.conv.weights_shape
        input_shape = self.conv.input_shape
        i_row_wid = int(input_shape[1])
        img_data_size = 1

        coef_group = 1 if i_row_wid > 32 else (2 if i_row_wid > 16 else 4)

        # io
        i_ch_num = int(weights_shape[2])
        o_ch_num = int(output_shape[3])
        # img o
        o_row_wid = int(output_shape[2])
        o_col_high = int(output_shape[1])
        wb_group = 1 if o_row_wid > 32 else (2 if o_row_wid > 16 else 4)
        wb_row_switch_addr = math.ceil(o_row_wid / 64)
        wb_channel_switch_addr = o_col_high * wb_row_switch_addr
        channel_byte_num = o_row_wid * o_col_high

        int_en = 0
        image_src_addr = None
        image_dst_addr = None
        dma_total_byte = o_row_wid * o_col_high * o_ch_num
        dma_burst_size = 0xf
        send_data_out = 0
        return locals()


def make_k210_layer(iwo_minmax, ico_shapes, conv_weights_isdw, bn_mean_var_gamma_beta_epsilon, act_type,
                    pool_type_size_stride, eight_bit_mode=False, cbap_tensor_info=None):
    input_min, input_max, weights_min, weights_max, output_min, output_max = iwo_minmax
    input_shape, conv_shape, output_shape = ico_shapes
    conv_weights, conv_isdw = conv_weights_isdw
    conv_tensor_info, bn_tensor_info, act_tensor_info, pool_tensor_info, *_ = [
        *list(cbap_tensor_info or []), dict(), dict(), dict(), dict()
    ]

    ret = K210Layer(eight_bit_mode)
    ret.conv = K210Conv(
        conv_weights,
        conv_isdw,
        eight_bit_mode, [input_shape, conv_shape],
        [input_min, input_max, weights_min, weights_max],
        tensor_info=conv_tensor_info
    )

    bn_mean, bn_var, bn_gamma, bn_beta, bn_epsilon = bn_mean_var_gamma_beta_epsilon
    ret.bn = K210BN(
        bn_mean,
        bn_var,
        bn_gamma,
        bn_beta,
        bn_epsilon,
        eight_bit_mode,
        tensor_info=bn_tensor_info
    )

    ret.act = K210Act(output_min, output_max, act_type,
                      eight_bit_mode=eight_bit_mode, tensor_info=act_tensor_info)

    if pool_type_size_stride is not None:
        pool_type, pool_size, pool_stride = pool_type_size_stride
        ret.pool = K210Pool(pool_type, pool_size, pool_stride, pool_tensor_info)

    return ret


def make_k210_layer_from_tensor(sess, dataset, buffer, input_min, input_max, eight_bit_mode, range_from_batch):
    pool_tensor_info = {}
    pool_type_size_stride = None  # bypass pool

    if isinstance(buffer[-1], tensor_list_to_layer_list.LayerConvolutional) \
            or isinstance(buffer[-1], tensor_list_to_layer_list.LayerDepthwiseConvolutional):
        conv_layer = buffer.pop()

        input_shape = list(sess.run(conv_layer.tensor_conv_x, dataset).shape)
        conved_shape = list(sess.run(conv_layer.tensor_conv_y, dataset).shape)

        # hotfix stride=2
        if conv_layer.tensor_conv_y.op.get_attr('strides')[1] == 2:
            conved_shape[1:3] = [conved_shape[1] * 2, conved_shape[2] * 2]
            input_shape[1:3] = conved_shape[1:3]

        weights_min, weights_max, _ = range_from_batch(sess, conv_layer.tensor_conv_w, dataset, is_weights=True)
        conv_weights = conv_layer.weights
        conv_isdw = isinstance(conv_layer, tensor_list_to_layer_list.LayerDepthwiseConvolutional)
        conv_tensor_info = {'name': conv_layer.tensor_conv_y.name}

        if int(conv_layer.config['batch_normalize']) == 1:
            bn_mean_var_gamma_beta_epsilon = [
                conv_layer.batch_normalize_moving_mean,
                conv_layer.batch_normalize_moving_variance,
                conv_layer.batch_normalize_gamma,
                conv_layer.batch_normalize_beta,
                conv_layer.batch_normalize_epsilon
            ]
            bn_tensor_info = {'name': 'bn'}
        else:
            bias_shape = conv_layer.bias.shape
            bn_mean_var_gamma_beta_epsilon = [
                0, 1, np.ones(bias_shape), conv_layer.bias, 0
            ]
            bn_tensor_info = {'name': 'bn'}

        tensor_act = conv_layer.tensor_activation
        act_min_y, act_max_y, _ = range_from_batch(sess, tensor_act, dataset)
        act_type = conv_layer.config['activation']
        act_tensor_info = {'name', tensor_act.name if tensor_act is not None else 'default_linear'}
        output_shape = tensor_act.shape
    else:
        raise ValueError('unsupported type seq: ', *[type(l) for l in buffer])

    if len(buffer) > 0 and isinstance(buffer[-1], tensor_list_to_layer_list.LayerPool):
        pool_layer = buffer.pop()
        assert (isinstance(pool_layer, tensor_list_to_layer_list.LayerPool))
        pool_size = pool_layer.config['size']
        pool_stride = pool_layer.config['stride']
        pool_type = pool_layer.tensor_pool.op.type
        # hotfix
        if pool_stride == 1 and conv_layer.config['stride'] == 2:
            pool_size = 2

        if pool_size == 2 and pool_layer.tensor_pool.op.inputs[0].shape[3] % 2 != 0:
            if pool_layer.tensor_pool.op.get_attr('padding') == b'SAME':
                raise ValueError("at {} unsupport padding mode SAME of pooling with size == 2" \
                                 .format(pool_layer.tensor_pool.name))

        pool_type_size_stride = [pool_type, pool_size, pool_stride]
        pool_tensor_info = {'name': pool_layer.tensor_pool.op.name}

    # hotfix
    elif conv_layer.config['stride'] == 2:
        pool_size = 2
        pool_stride = 2
        pool_type = 'hotfix_leftPool'
        pool_type_size_stride = [pool_type, pool_size, pool_stride]
        pool_tensor_info = {'name': 'hotfix_pool_for_conv_stride2'}
        output_shape = [output_shape[0], int(output_shape[1]) / 2, int(output_shape[2]) / 2, output_shape[3]]

    return make_k210_layer(
        iwo_minmax=[input_min, input_max, weights_min, weights_max, act_min_y, act_max_y],
        ico_shapes=[input_shape, conved_shape, output_shape],
        conv_weights_isdw=[conv_weights, conv_isdw],
        bn_mean_var_gamma_beta_epsilon=bn_mean_var_gamma_beta_epsilon,
        act_type=act_type,
        pool_type_size_stride=pool_type_size_stride,
        eight_bit_mode=eight_bit_mode,
        cbap_tensor_info=[conv_tensor_info, bn_tensor_info, act_tensor_info, pool_tensor_info]
    )


def k210_layer_post_fix(klayer: K210Layer):
    return klayer


def gen_k210_layers(layers: [tensor_list_to_layer_list.LayerBase], sess, dataset, range_from_batch,
                    eight_bit_mode=False, input_min=0, input_max=1):
    buffer = list(layers)
    buffer.reverse()
    ret = []

    net = buffer.pop()
    assert (isinstance(net, tensor_list_to_layer_list.LayerNet))

    while len(buffer) != 0:
        if len(ret) > 0:
            last_act = ret[-1].act
            last_min = last_act.min_y
            last_max = last_act.max_y
        else:
            last_min = input_min
            last_max = input_max

        cur_k210 = make_k210_layer_from_tensor(
            sess=sess, dataset=dataset,
            buffer=buffer,
            input_min=last_min, input_max=last_max,
            eight_bit_mode=eight_bit_mode,
            range_from_batch=range_from_batch
        )

        cur_k210_fixed = k210_layer_post_fix(cur_k210)
        ret.append(cur_k210_fixed) if isinstance(cur_k210_fixed, K210Layer) else ret.extend(cur_k210_fixed)

    return ret
