# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# pylint: disable=invalid-name, unused-argument, too-many-lines, import-outside-toplevel
# pylint: disable=unused-variable, no-else-return, eval-used, multiple-statements
# pylint: disable=consider-using-enumerate, logging-format-interpolation
# pylint: disable=no-else-continue
"""Caffe frontend."""
from __future__ import absolute_import as _abs
import logging
import numpy as np
import tvm
from tvm.ir import IRModule

from ... import nd as _nd
from .. import analysis
from .. import expr as _expr
from .. import function as _function
from .. import op as _op
from .common import ExprTable
from .common import infer_shape as _infer_shape
from .common import AttrCvt
from .common import set_span

__all__ = ["from_caffe"]


class OperatorConverter(object):
    """Operator Converted for converting Caffe ops to Relay ops"""

    def __init__(self, init_layer_dict, predict_layer, exp_tab):
        self.init_layer_dict = init_layer_dict
        self.predict_layer = predict_layer
        self.exp_tab = exp_tab
        self.new_bn = {}
        self.changed_layers = None

        self.convert_map = {
            "BatchNorm": self.batch_norm,
            "BN": self.seg_bn,
            "Concat": self.concat,
            "Convolution": self.conv,
            "DepthwiseConvolution": self.conv,
            "Crop": self.crop,
            "Deconvolution": self.deconv,
            "Dropout": self.dropout,
            "Eltwise": self.eltwise,
            "Flatten": self.flatten,
            "InnerProduct": self.innerproduct,
            "Input": None,
            "LRN": self.lrn,
            "Normalize": self.normalize,
            "Permute": self.permute,
            "Pooling": self.pooling,
            "PReLU": self.prelu,
            "PriorBox": self.priorbox,
            "proposal": self.proposal,
            "PSROIPooling": self.psroipooling,
            "Python": self.python_layer,
            "ReLU": self.relu,
            "Reshape": self.reshape,
            "Resize": self.resize,
            "ROIPooling": self.roipooling,
            "Scale": self.scale,
            "Sigmoid": self.sigmoid,
            "Slice": self._slice,
            "Softmax": self.softmax,
            "TanH": self.tanh,
            "Upsample": self.upsample,
            "Power": self.power,
        }

    def seg_bn(self, op):
        """ Convert BN layer, which is defined in: https://github.com/alexgkendall/caffe-segnet"""
        inputs = op.bottom

        bn_param = op.bn_param
        in_expr = self.exp_tab.get_expr(inputs[0])

        bn_blobs = self.init_layer_dict[op.name].blobs
        scale = np.asarray(bn_blobs[0].data, np.float32)
        shift = np.asarray(bn_blobs[1].data, np.float32)

        scale = np.reshape(scale, bn_blobs[0].shape.dim)
        shift = np.reshape(shift, bn_blobs[1].shape.dim)

        scale_expr = self.exp_tab.new_const(scale, dtype="float32")
        shift_expr = self.exp_tab.new_const(shift, dtype="float32")

        out = _op.multiply(in_expr, scale_expr)
        out = _op.add(out, shift_expr)

        return out

    def resize(self, op):
        """Convert Resize layer"""
        inputs = op.bottom

        # obtain layer params
        resize_param = op.img_size_param
        x_scale = float(resize_param.x_scaling)
        y_scale = float(resize_param.y_scaling)

        # get input expr
        in_expr = self.exp_tab.get_expr(inputs[0])

        # set tvm op params
        params = dict()
        params["scale_h"] = y_scale
        params["scale_w"] = x_scale
        params["layout"] = "NCHW"
        params["method"] = "bilinear"
        params["align_corners"] = False

        out = AttrCvt(op_name="upsampling")([in_expr], params)

        return out

    def upsample(self, op):
        """Convert Unsample layer"""
        inputs = op.bottom
        upsample_param = op.upsample_param

        scale = float(upsample_param.scale)

        # get input expr
        in_expr = self.exp_tab.get_expr(inputs[0])
        # set params
        params = dict()
        params["layout"] = "NCHW"
        if len(inputs) == 1:
            params["scale_h"] = scale
            params["scale_w"] = scale
            params["method"] = "nearest_neighbor"
            params["align_corners"] = False
            out = AttrCvt(op_name="upsampling")([in_expr], params)
        elif len(inputs) == 2:
            mask_expr = self.exp_tab.get_expr(inputs[1])
            params["scale_h"] = int(scale)
            params["scale_w"] = int(scale)
            out = AttrCvt(op_name="vision.unpooling")([in_expr, mask_expr], params)

        return out

    def python_layer(self, op):
        """Convert Python layer"""
        inputs = op.bottom
        python_params = op.python_param

        curr_layer_name = python_params.layer.lower()
        if curr_layer_name == "proposallayer":
            param_dict = {}
            param_str = python_params.param_str
            if "{" in param_str:
                param_dict = eval(param_str)
            else:
                param_str = "{" + param_str + "}"
                param_dict = eval(param_str)
            # get input expr
            rpn_cls_prob_expr = self.exp_tab.get_expr(inputs[0])
            rpn_bbox_pred_expr = self.exp_tab.get_expr(inputs[1])
            im_info_expr = self.exp_tab.get_expr(inputs[2])

            # set tvm proposal params
            proposal_params_tvm = dict()
            proposal_params_tvm["scales"] = (
                param_dict["scales"] if "scales" in param_dict else [4.0, 8.0, 16.0, 32.0]
            )
            proposal_params_tvm["ratios"] = (
                param_dict["ratios"] if "ratios" in param_dict else [0.5, 1.0, 2.0]
            )
            proposal_params_tvm["feature_stride"] = (
                param_dict["feat_stride"] if "feat_stride" in param_dict else 16
            )
            proposal_params_tvm["rpn_pre_nms_top_n"] = (
                param_dict["rpn_pre_nms_top_n"] if "rpn_pre_nms_top_n" in param_dict else 6000
            )
            proposal_params_tvm["rpn_post_nms_top_n"] = (
                param_dict["rpn_post_nms_top_n"] if "rpn_post_nms_top_n" in param_dict else 300
            )
            proposal_params_tvm["threshold"] = (
                param_dict["rpn_nms_thresh"] if "rpn_nms_thresh" in param_dict else 0.7
            )
            proposal_params_tvm["rpn_min_size"] = (
                param_dict["rpn_min_size"] if "rpn_min_size" in param_dict else 16
            )
            proposal_params_tvm["iou_loss"] = (
                param_dict["iou_loss"] if "iou_loss" in param_dict else False
            )

            out = AttrCvt(op_name="vision.proposal")(
                [rpn_cls_prob_expr, rpn_bbox_pred_expr, im_info_expr], proposal_params_tvm
            )
        else:
            tvm.error.OpNotImplemented("Python.{} has not been supported!".format(curr_layer_name))
        return out

    def psroipooling(self, op):
        """Convert PSROIPooling layer"""
        inputs = op.bottom
        psroi_pooling_param = op.psroi_pooling_param

        # get inputs expr
        rfcn_cls_expr = self.exp_tab.get_expr(inputs[0])
        rois_expr = self.exp_tab.get_expr(inputs[1])

        # set tvm params
        params = dict()
        params["spatial_scale"] = psroi_pooling_param.spatial_scale
        params["output_dim"] = psroi_pooling_param.output_dim
        params["group_size"] = psroi_pooling_param.group_size

        out = AttrCvt(op_name="vision.psroipooling")([rfcn_cls_expr, rois_expr], params)
        return out

    def permute(self, op):
        """Convert Permute layer"""
        inputs = op.bottom
        in_expr = self.exp_tab.get_expr(inputs[0])
        permute_params = op.permute_param.order

        out = AttrCvt(op_name="transpose")([in_expr], {"axes": permute_params})
        return out

    def power(self, op):
        """Convert power layer"""
        inputs = op.bottom
        in_expr = self.exp_tab.get_expr(inputs[0])
        power_params = op.power_param
        shift = power_params.shift
        scale = power_params.scale
        power = power_params.power
        out = _op.multiply(in_expr, _expr.const(scale))
        out = _op.add(out, _expr.const(shift))
        if power != 1:
            out = AttrCvt(op_name="power")([out, _expr.const([power])], {})

        return out

    def normalize(self, op):
        """Convert Normalize layer"""
        inputs = op.bottom
        in_expr = self.exp_tab.get_expr(inputs[0])
        in_shape = _infer_shape(in_expr)
        norm_params = op.norm_param

        across_spatial = norm_params.across_spatial
        channel_shared = norm_params.channel_shared
        eps = norm_params.eps

        scale_type = norm_params.scale_filler.type

        if channel_shared:
            scale = np.asarray(norm_params.scale_filler.value, np.float32)
            scale_expr = self.exp_tab.new_const(scale, dtype="float32")
        else:
            weight_bias_blobs = self.init_layer_dict[op.name].blobs
            scale = np.asarray(weight_bias_blobs[0].data, np.float32).reshape(
                (in_shape[0], in_shape[1], 1, 1)
            )
            scale_expr = self.exp_tab.new_const(scale, dtype="float32")
        if across_spatial:
            out = AttrCvt(op_name="l2_normalize")([in_expr], {"eps": eps})
        else:
            out = AttrCvt(op_name="l2_normalize")([in_expr], {"eps": eps, "axis": [1]})

        out = AttrCvt(op_name="multiply")([out, scale_expr], {})
        return out

    def add_box(self, top_data, x, y, width, height, img_width, img_height):
        """Generate box coordinate"""
        # xmin
        top_data.append((x - width / 2.0) / img_width)
        # ymin
        top_data.append((y - height / 2.0) / img_height)
        # xmax
        top_data.append((x + width / 2.0) / img_width)
        # ymax
        top_data.append((y + height / 2.0) / img_height)
        return top_data

    def priorbox(self, op):
        """Convert PriorBox layer"""
        inputs = op.bottom

        in_expr = self.exp_tab.get_expr(inputs[0])
        in_expr_img = self.exp_tab.get_expr(inputs[1])
        pre_n, pre_c, pre_h, pre_w = _infer_shape(in_expr)

        img_n, img_c, img_h, img_w = _infer_shape(in_expr_img)

        priorbox_params = op.prior_box_param
        flip = priorbox_params.flip
        variance = np.asarray(priorbox_params.variance)
        ratios = [1]
        for r in priorbox_params.aspect_ratio:
            if r != 1:
                ratios.append(r)
                if flip:
                    ratios.append(1 / r)

        min_size = priorbox_params.min_size
        max_size = priorbox_params.max_size

        steps = priorbox_params.step
        if steps:
            step_h = step_w = steps
        else:
            step_h = img_h / pre_h
            step_w = img_w / pre_w

        offsets = priorbox_params.offset
        clip = priorbox_params.clip
        num_priors_ = len(ratios)
        if max_size:
            for i in range(len(max_size)):
                num_priors_ += 1

        dim = pre_h * pre_w * num_priors_ * 4
        out = []
        for h in range(pre_h):
            for w in range(pre_w):
                center_x = (w + offsets) * step_w
                center_y = (h + offsets) * step_h
                for s in range(len(min_size)):
                    min_size_ = min_size[s]
                    box_width = box_height = min_size_
                    out = self.add_box(out, center_x, center_y, box_width, box_height, img_w, img_h)
                    if max_size:
                        assert len(max_size) == len(
                            min_size
                        ), "max_size and min_size should be same"
                        max_size_ = max_size[s]
                        box_width = box_height = np.sqrt(min_size_ * max_size_)
                        out = self.add_box(
                            out, center_x, center_y, box_width, box_height, img_w, img_h
                        )
                    for r in range(len(ratios)):
                        ar = ratios[r]
                        if ar != 1:
                            box_width = min_size_ * np.sqrt(ar)
                            box_height = min_size_ / np.sqrt(ar)
                            out = self.add_box(
                                out, center_x, center_y, box_width, box_height, img_w, img_h
                            )
        if clip:
            out = np.clip(out, 0, 1)
        if len(variance) == 1:
            variance_ = np.full(dim, variance[0], dtype="float32")
        else:
            variance_ = np.tile(variance, (int(dim / 4), 1))

        variance_ = variance_.reshape(-1)
        out = np.append(out, variance_)
        out = np.asarray(out, dtype="float32")
        out = self.exp_tab.new_const(out, dtype="float32")
        out = _op.reshape(out, (img_n, 2, -1))

        return out

    def flatten(self, op):
        """Convert Flatten layer"""
        inputs = op.bottom
        in_expr = self.exp_tab.get_expr(inputs[0])
        in_shape = _infer_shape(in_expr)

        flatten_params = op.flatten_param.axis
        assert flatten_params == 1, "flatten axis should be 1"
        out = AttrCvt(op_name="batch_flatten")([in_expr], {})
        return out

    def eltwise(self, op):
        """Convert Eltwise layer"""
        inputs = op.bottom
        assert len(inputs) == 2, "input tensors length should be 2"

        lhs_expr = self.exp_tab.get_expr(inputs[0])
        rhs_expr = self.exp_tab.get_expr(inputs[1])

        lhs_shape = _infer_shape(lhs_expr)
        rhs_shape = _infer_shape(rhs_expr)

        assert lhs_shape == rhs_shape, "input tensors shape should be equal"

        eltwise_params = op.eltwise_param
        eltwise_type_dict = ["PROD", "SUM", "MAX"]
        eltwise_type = eltwise_params.operation
        coeff = list(eltwise_params.coeff)

        if eltwise_type_dict[eltwise_type] == "PROD":
            out = AttrCvt(op_name="multiply")([lhs_expr, rhs_expr], {})
        elif eltwise_type_dict[eltwise_type] == "SUM":
            if coeff:
                left_coeff_expr = self.exp_tab.new_const(np.asarray(coeff[0], np.float32))
                right_coeff_expr = self.exp_tab.new_const(np.asarray(coeff[1], np.float32))
                lhs_expr_scale = AttrCvt(op_name="multiply")([lhs_expr, left_coeff_expr], {})
                rhs_expr_scale = AttrCvt(op_name="multiply")([rhs_expr, right_coeff_expr], {})
                out = AttrCvt(op_name="add")([lhs_expr_scale, rhs_expr_scale], {})

            else:
                out = AttrCvt(op_name="add")([lhs_expr, rhs_expr], {})

        elif eltwise_type_dict[eltwise_type] == "MAX":
            out = AttrCvt(op_name="maximum")([lhs_expr, rhs_expr], {})

        else:
            raise tvm.error.OpNotImplemented(
                "eltwise_type {} is not supported for frontend Caffe.".format(eltwise_type)
            )

        return out

    def _parse_conv_params(self, op):
        """Parse the parameters of Convolution and Deconvolution layer"""
        nonzone = lambda val, pos, dflt: val[pos] if pos < len(val) else dflt

        conv_params = op.convolution_param

        params = dict()
        # parse kernel size
        if conv_params.kernel_h > 0 or conv_params.kernel_w > 0:
            params["kernel_size"] = (conv_params.kernel_h, conv_params.kernel_w)
        else:
            ksize_h = nonzone(conv_params.kernel_size, 0, 1)
            ksize_w = nonzone(conv_params.kernel_size, 1, ksize_h)
            params["kernel_size"] = (ksize_h, ksize_w)

        # parse padding size
        if conv_params.pad_h > 0 or conv_params.pad_w > 0:
            params["padding"] = (conv_params.pad_h, conv_params.pad_w)
        else:
            pad_h = nonzone(conv_params.pad, 0, 0)
            pad_w = nonzone(conv_params.pad, 1, pad_h)
            params["padding"] = (pad_h, pad_w)

        # parse stride size
        if conv_params.stride_h > 0 or conv_params.stride_w > 0:
            params["strides"] = (conv_params.stride_h, conv_params.stride_w)
        else:
            stride_h = nonzone(conv_params.stride, 0, 1)
            stride_w = nonzone(conv_params.stride, 1, stride_h)
            params["strides"] = (stride_h, stride_w)

        # parse dilation size
        if hasattr(conv_params, "dilation") and len(conv_params.dilation) > 0:
            dilation = " ".join(str(d) for d in conv_params.dilation)
            dilation = tuple(map(int, dilation.split(" ")))
            params["dilation"] = dilation
            if len(dilation) == 1:
                params["dilation"] = (dilation[0], dilation[0])

        params["kernel_layout"] = "OIHW"
        params["data_layout"] = "NCHW"
        params["groups"] = conv_params.group
        params["channels"] = conv_params.num_output
        return params

    def batch_norm(self, op):
        """ Convert BatchNorm layer """
        inputs = op.bottom
        in_expr = self.exp_tab.get_expr(inputs[0])
        shape = _infer_shape(in_expr)
        if len(shape) == 2:
            n = c = 1
            h, w = shape
        else:
            n, c, h, w = shape
        if op.name in self.new_bn:
            mean, var, eps, gamma, beta = self.new_bn[op.name]
            if len(var[var < 0]) > 0:
                logging.warning("The negative numbers in BN variance are forced to replace 0!")
                var[var < 0] = 0
            mean_expr = self.exp_tab.new_const(mean, dtype="float32")
            var_expr = self.exp_tab.new_const(var, dtype="float32")
            gamma_expr = self.exp_tab.new_const(gamma, dtype="float32")
            beta_expr = self.exp_tab.new_const(beta, dtype="float32")
            out = AttrCvt(op_name="batch_norm")(
                [in_expr, gamma_expr, beta_expr, mean_expr, var_expr],
                {"epsilon": eps, "scale": True},
            )

        else:
            weight_bias_blobs = self.init_layer_dict[op.name].blobs
            mean = np.asarray(weight_bias_blobs[0].data, np.float32)
            var = np.asarray(weight_bias_blobs[1].data, np.float32)
            if len(weight_bias_blobs) == 2:
                mean = np.repeat(mean, h * w).reshape((c, h, w))
                mean = np.expand_dims(mean, 0).repeat(n, axis=0)
                mean_expr = self.exp_tab.new_const(mean, dtype="float32")

                var = np.repeat(var, h * w).reshape((c, h, w))
                var = np.expand_dims(var, 0).repeat(n, axis=0)
                var_expr = self.exp_tab.new_const(var, dtype="float32")

                tmp_out = AttrCvt(op_name="multiply")([in_expr, mean_expr], {})
                out = AttrCvt(op_name="add")([tmp_out, var_expr], {})
                return out
            else:
                scale = np.asarray(weight_bias_blobs[2].data, np.float32)
                if scale:
                    scale = 1 / scale
            mean_expr = self.exp_tab.new_const(mean * scale, dtype="float32")
            var_expr = self.exp_tab.new_const(var * scale, dtype="float32")

            # caffe bn layer not support scale
            gamma_expr = self.exp_tab.new_const(
                np.ones(mean.shape, dtype=np.float32), dtype="float32"
            )
            beta_expr = self.exp_tab.new_const(
                np.zeros(mean.shape, dtype=np.float32), dtype="float32"
            )

            bn_params = op.batch_norm_param.eps
            out = AttrCvt(op_name="batch_norm")(
                [in_expr, gamma_expr, beta_expr, mean_expr, var_expr],
                {"epsilon": bn_params, "scale": False},
            )

        return out[0]

    def scale(self, op):
        """Convert Scale layer"""
        inputs = op.bottom
        in_expr = self.exp_tab.get_expr(inputs[0])
        weight_bias_blobs = self.init_layer_dict[op.name].blobs

        params = dict()
        params["bias"] = op.scale_param.bias_term
        params["axis"] = op.scale_param.axis

        n, c, h, w = _infer_shape(in_expr)
        gamma = np.asarray(weight_bias_blobs[0].data, np.float32)
        gamma = np.reshape(gamma, (1, c, 1, 1))
        gamma_expr = self.exp_tab.new_const(gamma, dtype="float32")
        if params["bias"]:
            beta = np.asarray(weight_bias_blobs[1].data, np.float32)
            beta = np.reshape(beta, (1, c, 1, 1))
            beta_expr = self.exp_tab.new_const(beta, dtype="float32")
        else:
            beta_expr = self.exp_tab.new_const(
                np.zeros(gamma.shape, dtype=np.float32), dtype="float32"
            )

        out = AttrCvt(op_name="multiply")([in_expr, gamma_expr], {})
        out = AttrCvt(op_name="add")([out, beta_expr], {})
        return out

    def concat(self, op):
        """Convert Concat layer"""
        inputs = op.bottom
        in_expr = tuple((self.exp_tab.get_expr(inputs[i]) for i in range(len(inputs))))

        params = dict()
        params["axis"] = op.concat_param.axis
        if len(inputs) == 1:
            out = AttrCvt(op_name="reshape")(
                [in_expr[0]], {"newshape": list(_infer_shape(in_expr[0]))}
            )
        else:
            out = AttrCvt(op_name="concatenate")([in_expr], {"axis": params["axis"]})

        return out

    def reshape(self, op):
        """Convert Reshape layer"""
        inputs = op.bottom
        input_name = inputs[0]

        reshape_param = op.reshape_param
        dims = list(reshape_param.shape.dim)

        in_expr = self.exp_tab.get_expr(input_name)
        input_shape = list(_infer_shape(in_expr))

        start_axis = int(reshape_param.axis)
        if start_axis < 0:
            start_axis = len(input_shape) + start_axis + 1
        num_axes = int(reshape_param.num_axes)
        end_axis = len(input_shape)
        if num_axes != -1:
            end_axis = start_axis + num_axes

        left_shape = input_shape[:start_axis]
        if end_axis == len(input_shape):
            center_shape = input_shape[start_axis:]
            right_shape = []
        else:
            center_shape = input_shape[start_axis:end_axis]
            right_shape = input_shape[end_axis:]

        for idx, dim in enumerate(dims):
            if dim == 0:
                dims[idx] = center_shape[idx]

        tmp = np.random.rand(*center_shape)
        tmp = np.reshape(tmp, dims)
        center_shape = list(tmp.shape)

        newshape = left_shape + center_shape + right_shape

        out = AttrCvt(op_name="reshape")([in_expr], {"newshape": newshape})
        return out

    def softmax(self, op):
        """Convert Softmax layer"""
        inputs = op.bottom
        assert len(inputs) == 1, "input tensors length should be 1"

        input_name = inputs[0]
        in_expr = self.exp_tab.get_expr(input_name)

        softmax_param = op.softmax_param
        params = {"axis": softmax_param.axis}

        out = AttrCvt(op_name="softmax")([in_expr], params)

        return out

    def conv(self, op):
        """Convert Convolution layer"""
        params = self._parse_conv_params(op)
        weight_bias_blobs = self.init_layer_dict[op.name].blobs
        conv_params = op.convolution_param
        inputs = op.bottom
        # process weight and bias blobs
        weight, bias = None, None
        if len(weight_bias_blobs) > 1:
            weight = weight_bias_blobs[0]
            bias = weight_bias_blobs[1]
        else:
            weight = weight_bias_blobs[0]
        if weight:
            kh, kw = params["kernel_size"]
            weight_shape = [conv_params.num_output, -1, kh, kw]
            weight_value = np.asarray(weight.data, np.float32)
            weight_value = np.reshape(weight_value, weight_shape)
        else:
            raise Exception("No weight value of layer {} in caffemodel".format(op.name))

        weight_expr = self.exp_tab.new_const(weight_value, dtype="float32")
        in_expr = self.exp_tab.get_expr(inputs[0])
        out = AttrCvt(op_name="conv2d")([in_expr, weight_expr], params)

        if bias:
            bias_value = np.asarray(bias.data, np.float32)
            bias_expr = self.exp_tab.new_const(bias_value, dtype="float32")
            out = AttrCvt(op_name="bias_add")([out, bias_expr], {})
        return out

    def pooling(self, op):
        """Convert Pooling layer"""
        inputs = op.bottom
        input_name = inputs[0]

        pool_params = op.pooling_param
        pool_type_dict = ["MAX", "AVE", "STOCHASTIC"]

        params = dict()
        # parse pool type: 0: MAX, 1: AVE, 2: STOCHASTIC
        pool_type = pool_params.pool
        # parse kernel size
        if pool_params.kernel_h > 0 or pool_params.kernel_w > 0:
            params["pool_size"] = (pool_params.kernel_h, pool_params.kernel_w)
        else:
            params["pool_size"] = (pool_params.kernel_size, pool_params.kernel_size)

        # parse padding size
        if pool_params.pad_h > 0 or pool_params.pad_w > 0:
            params["padding"] = (pool_params.pad_h, pool_params.pad_w)
        else:
            params["padding"] = (pool_params.pad, pool_params.pad)

        # parse stride size
        if pool_params.stride_h > 0 or pool_params.stride_w > 0:
            params["strides"] = (pool_params.stride_h, pool_params.stride_w)
        else:
            params["strides"] = (pool_params.stride, pool_params.stride)

        params["ceil_mode"] = True
        if hasattr(pool_params, "ceil_mode"):
            params["ceil_mode"] = pool_params.ceil_mode

        in_expr = self.exp_tab.get_expr(input_name)

        if pool_type_dict[pool_type] == "MAX":
            if pool_params.global_pooling:
                out = AttrCvt(op_name="global_max_pool2d")([in_expr], {})
            else:
                if len(op.top) == 1:
                    out = AttrCvt(op_name="max_pool2d")([in_expr], params)
                elif len(op.top) == 2:
                    out1 = AttrCvt(op_name="max_pool2d_with_argmax")([in_expr], params)
                    out2 = AttrCvt(op_name="vision.max_pool2d_location")([in_expr], params)
                    return _expr.Tuple((out1, out2))

        elif pool_type_dict[pool_type] == "AVE":  # AVE
            if pool_params.global_pooling:
                out = AttrCvt(op_name="global_avg_pool2d")([in_expr], {})
            else:
                params["count_include_pad"] = True
                out = AttrCvt(op_name="avg_pool2d")([in_expr], params)

        else:
            raise tvm.error.OpNotImplemented(
                "Operator {} is not supported for frontend Caffe.".format(
                    pool_type_dict[pool_type] + " pool"
                )
            )

        return out

    def lrn(self, op):
        """Convert LRN layer"""
        inputs = op.bottom
        input_name = inputs[0]

        params = dict()
        lrn_params = op.lrn_param
        params["size"] = lrn_params.local_size
        params["bias"] = lrn_params.k
        params["alpha"] = lrn_params.alpha
        params["beta"] = lrn_params.beta
        params["norm_region"] = (
            "ACROSS_CHANNELS" if lrn_params.norm_region == 0 else "WITHIN_CHANNEL"
        )

        in_expr = self.exp_tab.get_expr(input_name)
        out = AttrCvt(op_name="lrn")([in_expr], params)

        return out

    def innerproduct(self, op):
        """Convert InnerProduct layer"""
        inputs = op.bottom
        weight_bias_blobs = self.init_layer_dict[op.name].blobs
        dense_params = op.inner_product_param

        params = dict()
        params["num_output"] = dense_params.num_output
        params["bias"] = dense_params.bias_term
        params["axis"] = dense_params.axis
        if params["axis"] != 1:
            raise Exception("Only support 2D InnerProduct")

        # process weight and bias blobs
        weight, bias = None, None
        if params["bias"]:
            weight = weight_bias_blobs[0]
            bias = weight_bias_blobs[1]
        else:
            weight = weight_bias_blobs[0]

        if weight:
            weight_value = np.asarray(weight.data, np.float32)
            weight_value = np.reshape(weight_value, (params["num_output"], -1))
            weight_shape = weight_value.shape
        else:
            raise Exception("No weight value of layer {} in caffemodel".format(op.name))

        weight_expr = self.exp_tab.new_const(weight_value, dtype="float32")

        in_expr = self.exp_tab.get_expr(inputs[0])
        in_reshape = AttrCvt(op_name="reshape")([in_expr], {"newshape": (-1, weight_shape[-1])})

        out = AttrCvt(op_name="dense", extras={"units": params["num_output"]})(
            [in_reshape, weight_expr], {}
        )

        if bias:
            bias_value = np.asarray(bias.data, np.float32)
            bias_expr = self.exp_tab.new_const(bias_value, dtype="float32")
            out = AttrCvt(op_name="bias_add")([out, bias_expr], {"axis": params["axis"]})
        return out

    def dropout(self, op):
        """Convert Dropout layer"""
        inputs = op.bottom
        input_name = inputs[0]

        params = dict()
        dropout_params = op.dropout_param

        params["rate"] = dropout_params.dropout_ratio

        in_expr = self.exp_tab.get_expr(input_name)
        out = AttrCvt(op_name="dropout")([in_expr], params)
        return out

    def relu(self, op):
        """Convert ReLU layer"""
        inputs = op.bottom
        in_expr = self.exp_tab.get_expr(inputs[0])
        negative_slope = op.relu_param.negative_slope
        if negative_slope:
            out = AttrCvt(op_name="leaky_relu")([in_expr], {"alpha": negative_slope})

            return out

        out = AttrCvt(op_name="relu")([in_expr], {})

        return out

    def prelu(self, op):
        """Convert PReLU layer"""
        inputs = op.bottom
        in_expr = self.exp_tab.get_expr(inputs[0])

        alpha = self.init_layer_dict[op.name].blobs[0].data
        alpha = np.asarray(alpha, np.float32)
        alpha = self.exp_tab.new_const(alpha, dtype="float32")
        axis = 1
        out = AttrCvt(op_name="prelu")([in_expr, alpha], {"axis": axis})
        return out

    def deconv(self, op):
        """Convert Deconvolution layer"""
        params = self._parse_conv_params(op)
        params["kernel_layout"] = "IOHW"
        weight_bias_blobs = self.init_layer_dict[op.name].blobs
        inputs = op.bottom
        in_expr = self.exp_tab.get_expr(inputs[0])
        in_shape = _infer_shape(in_expr)
        # process weight and bias blobs
        weight, bias = None, None
        if len(weight_bias_blobs) > 1:
            weight = weight_bias_blobs[0]
            bias = weight_bias_blobs[1]
        else:
            weight = weight_bias_blobs[0]
        if weight:
            weight_shape = list(weight.shape.dim)
            weight_value = np.asarray(weight.data, np.float32)
            weight_value = np.reshape(weight_value, weight_shape)

            # # weight shape is in relay's IOHW format rn, we need it to be OIHW
            # weight_value = np.transpose(weight_value, [1, 0, 2, 3])
        else:
            raise Exception("No weight value of layer {} in caffemodel".format(op.name))

        weight_expr = self.exp_tab.new_const(weight_value, dtype="float32")
        out = AttrCvt(op_name="conv2d_transpose")([in_expr, weight_expr], params)

        if bias:
            bias_value = np.asarray(bias.data, np.float32)
            bias_expr = self.exp_tab.new_const(bias_value, dtype="float32")
            out = AttrCvt(op_name="bias_add")([out, bias_expr], {})
        return out

    def _slice(self, op):
        """Convert Slice layer"""
        inputs = op.bottom
        in_expr = self.exp_tab.get_expr(inputs[0])

        output_num = len(op.top)

        slice_params = op.slice_param
        axis = int(slice_params.axis)
        indices_or_sections = list([int(s) for s in slice_params.slice_point])
        if len(indices_or_sections) == 0:
            indices_or_sections = output_num
        else:
            indices_or_sections = sorted(indices_or_sections)

        out = AttrCvt(op_name="split")(
            [in_expr], {"indices_or_sections": indices_or_sections, "axis": axis}
        )
        return out

    def sigmoid(self, op):
        """Convert Sigmoid layer"""
        inputs = op.bottom
        in_expr = self.exp_tab.get_expr(inputs[0])
        out = AttrCvt(op_name="sigmoid")([in_expr], {})
        return out

    def tanh(self, op):
        """Convert TanH layer"""
        inputs = op.bottom
        in_expr = self.exp_tab.get_expr(inputs[0])
        out = AttrCvt(op_name="tanh")([in_expr], {})
        return out

    def crop(self, op):
        """Convert Crop layer"""
        inputs = op.bottom
        assert len(inputs) == 2, "Need two inputs of Crop layer"
        in_expr_a = self.exp_tab.get_expr(inputs[0])
        in_expr_b = self.exp_tab.get_expr(inputs[1])

        # parse crop params
        crop_params = op.crop_param
        axis = int(getattr(crop_params, "axis", 2))
        offset = list(getattr(crop_params, "offset", 0))

        # expand offset to (offset1, offset2, ...)
        in_a_shape = _infer_shape(in_expr_a)
        in_b_shape = _infer_shape(in_expr_b)
        if in_a_shape == in_b_shape:
            return in_expr_a
        num_to_crop = len(in_a_shape) - axis
        if not offset:
            offset = [0] * num_to_crop
        if len(offset) == 1:
            offset = offset * num_to_crop
        elif len(offset) != num_to_crop:
            raise Exception("No matching the number between axis and offset!")

        slice_end = list(in_a_shape)
        slice_start = [0] * len(in_a_shape)
        for i in range(num_to_crop):
            slice_start[i + axis] = offset[i]
            slice_end[i + axis] = offset[i] + in_b_shape[i + axis]

        to_crop_axis = list(range(len(in_a_shape)))
        to_crop_axis = to_crop_axis[axis:]

        # secondly, crop in_expr_a by in_expr_b
        out = AttrCvt(op_name="strided_slice")(
            [in_expr_a], {"begin": slice_start, "end": slice_end}
        )
        return out

    def proposal(self, op):
        """Convert proposal layer"""
        inputs = op.bottom
        assert len(inputs) == 2, "Need two inputs of proposal layer"
        rpn_cls_prob_expr = self.exp_tab.get_expr(inputs[0])
        rpn_bbox_pred = self.exp_tab.get_expr(inputs[1])

        model_input = self.predict_layer[0].top[0]
        n, c, h, w = _infer_shape(self.exp_tab.get_expr(model_input))
        im_info = np.array([h, w, 1], dtype=np.float32)
        im_info = np.tile(im_info, (n, 1))
        im_info_expr = self.exp_tab.new_const(im_info, dtype="float32")

        proposal_params = op.proposal_param
        params = dict()
        if hasattr(proposal_params, "scale") and len(proposal_params.scale) > 0:
            params["scales"] = list(float(s) for s in proposal_params.scale)
        if hasattr(proposal_params, "ratio") and len(proposal_params.ratio) > 0:
            params["ratios"] = list(float(r) for r in proposal_params.ratio)
        if hasattr(proposal_params, "base_size"):
            pass
        if hasattr(proposal_params, "feat_stride"):
            params["feature_stride"] = int(proposal_params.feat_stride)
        if hasattr(proposal_params, "pre_nms_topn"):
            params["rpn_pre_nms_top_n"] = int(proposal_params.pre_nms_topn)
        if hasattr(proposal_params, "post_nms_topn"):
            params["rpn_post_nms_top_n"] = int(proposal_params.post_nms_topn)
        if hasattr(proposal_params, "nms_thresh"):
            params["threshold"] = float(proposal_params.nms_thresh)
        if hasattr(proposal_params, "min_size"):
            params["rpn_min_size"] = int(proposal_params.min_size)
        params["iou_loss"] = False

        out = AttrCvt(op_name="vision.proposal")(
            [rpn_cls_prob_expr, rpn_bbox_pred, im_info_expr], params
        )
        return out
        # out_score = AttrCvt(op_name="zeros")([], {"shape": (n, 1), "dtype": "float32"})
        # return out, out_score

    def roipooling(self, op):
        """Convert ROIPooling layer"""
        inputs = op.bottom
        conv_feature_expr = self.exp_tab.get_expr(inputs[0])
        proposal_expr = self.exp_tab.get_expr(inputs[1])

        roipooling_params = op.roi_pooling_param
        params = dict()
        params["pooled_size"] = (int(roipooling_params.pooled_h), int(roipooling_params.pooled_w))
        params["spatial_scale"] = float(roipooling_params.spatial_scale)

        out = AttrCvt(op_name="vision.roi_pool")([conv_feature_expr, proposal_expr], params)
        return out

    def convert_embed(self, op):
        """Convert Embed layer"""
        inputs = op.bottom
        embed_param = op.embed_param
        num_output = embed_param.num_output
        input_dim = embed_param.input_dim
        bias_term = embed_param.bias_term
        weight_bias_blobs = self.init_layer_dict[op.name].blobs
        weight, bias = None, None
        if bias_term:
            weight = weight_bias_blobs[0]
            bias = weight_bias_blobs[1]
            assert weight and bias
        else:
            weight = weight_bias_blobs[0]
            assert weight
        weight_value = np.asarray(weight.data, np.float32)
        weight_value = np.reshape(weight_value, [input_dim, num_output])
        weight_expr = self.exp_tab.new_const(weight_value, dtype="float32")
        in_expr = self.exp_tab.get_expr(inputs[0])
        input_shape = _infer_shape(in_expr)
        input_count = 1
        for dim in input_shape:
            input_count *= dim

        index = _op.cast(in_expr, "int32")
        out = _op.take(weight_expr, index, axis=0)

        if bias_term:
            bias_value = np.asarray(bias.data, np.float32)
            bias_expr = self.exp_tab.new_const(bias_value, dtype="float32")
            out = _op.reshape(out, [input_count, num_output])
            out = _op.add(out, bias_expr)

        out_shape = list(input_shape)
        out_shape.append(num_output)
        out = _op.reshape(out, out_shape)

        return out

    def check_unsupported_ops(self):
        """Check unsupported Caffe ops in our converter."""
        logging.debug("check unsupported ops")
        unsupported_ops_set = set()

        include_layer = dict()
        for pl in self.predict_layer:
            if pl.type not in include_layer:
                include_layer[pl.type] = 1
            else:
                include_layer[pl.type] = include_layer[pl.type] + 1
        logging.debug("include layers: {}".format(include_layer.items()))

        for pl in self.predict_layer:
            op_name = pl.type
            if op_name not in self.convert_map:
                unsupported_ops_set.add(op_name)

        if unsupported_ops_set:
            msg = "The following operators are not supported in frontend " "Caffe: {}"
            ops = str(list(unsupported_ops_set)).strip("[,]")
            raise tvm.error.OpNotImplemented(msg.format(ops))

    def fuse_op(self, layers):
        """Fusing the BatchNorm and Scale layer"""
        bn, scale = layers["bn"], layers["scale"]

        # bn params
        bn_weight_bias_blobs = self.init_layer_dict[bn.name].blobs
        bn_scale = np.asarray(bn_weight_bias_blobs[2].data, np.float32)
        if bn_scale:
            bn_scale = 1 / bn_scale
        bn_mean = np.asarray(bn_weight_bias_blobs[0].data, np.float32) * bn_scale
        bn_var = np.asarray(bn_weight_bias_blobs[1].data, np.float32) * bn_scale
        bn_eps = bn.batch_norm_param.eps

        # scale params
        scale_weight_bias_blobs = self.init_layer_dict[scale.name].blobs
        scale_gamma = np.asarray(scale_weight_bias_blobs[0].data, np.float32)
        scale_bias = scale.scale_param.bias_term
        if scale_bias:
            scale_beta = np.asarray(scale_weight_bias_blobs[1].data, np.float32)
        else:
            scale_beta = np.zeros(scale_gamma.shape, dtype=np.float32)

        # new params
        self.new_bn[bn.name] = [bn_mean, bn_var, bn_eps, scale_gamma, scale_beta]
        return bn

    def op_fuse(self):
        """fuse bn and scale"""
        logging.debug("Caffe:fuse bn and scale")
        new_layers = []
        temp_layers = {}
        changed_layers = {}

        for index, pl in enumerate(self.predict_layer):
            op_type = pl.type
            if op_type == "Input":
                new_layers.append(pl)
                continue
            elif op_type == "BatchNorm":
                if (index != len(self.predict_layer) - 1) and (
                    self.predict_layer[index + 1].type == "Scale"
                ):
                    temp_layers["bn"] = pl
                    continue
                else:
                    new_layers.append(pl)
                    temp_layers.clear()
            elif op_type == "Scale":
                if self.predict_layer[index - 1].type == "BatchNorm":
                    temp_layers["scale"] = pl
                else:
                    new_layers.append(pl)
                    temp_layers.clear()
            else:
                temp_layers.clear()

            if len(temp_layers) == 2:
                layer = self.fuse_op(temp_layers)
                new_layers.append(layer)
                if len(temp_layers["bn"].top) == 1:
                    changed_layers[temp_layers["scale"].name] = temp_layers["bn"].top[0]
                else:
                    changed_layers[temp_layers["scale"].name] = temp_layers["bn"].name
            for idx, plt in enumerate(pl.bottom):
                if plt in changed_layers:
                    pl.bottom[idx] = changed_layers[plt]

            if op_type not in ["BatchNorm", "Scale"]:
                new_layers.append(pl)

        self.predict_layer = new_layers
        self.changed_layers = changed_layers

    def convert_op_to_relay(self):
        """Convert Caffe ops to relay ops"""
        logging.debug("convert op to relay")

        for pl in self.predict_layer:
            op_type = pl.type
            if op_type == "Input":
                continue
            output_tensors = pl.top

            ret = self.convert_map[op_type](pl)
            ret = set_span(ret, pl.name)

            if len(output_tensors) == 1:
                self.exp_tab.set_expr(output_tensors[0], ret)
                logging.debug(
                    "layer_name:{}, type:{}, output_name:{}, shape:{}".format(
                        pl.name, pl.type, output_tensors[0], _infer_shape(ret)
                    )
                )
            else:
                for idx, output_tensor in enumerate(output_tensors):
                    self.exp_tab.set_expr(output_tensor, ret[idx])
                    logging.debug(
                        "layer_name:{}, type:{}, output_name:{}, shape:{}".format(
                            pl.name, pl.type, output_tensor, _infer_shape(ret[idx])
                        )
                    )


def _rebuild_layers(predict_layer):
    """Rebuild caffe layer. If the the caffe net include in-place layers, repalce its top
    with its name and update the bottom of other layer that is related to it.
    """
    # dict of input name that will be changed to new name
    changed_top_dict = dict()

    top_change_before_dict = dict()

    for pl in predict_layer:
        if pl.type == "Input":
            continue
        # if current layer has single input and output and input equals to output
        # it means that the layer does "in-place"
        if len(pl.top) == 1 and len(pl.bottom) == 1:
            if pl.top[0] == pl.bottom[0]:
                # change current layer's input firstly
                if pl.bottom[0] in changed_top_dict:
                    pl.bottom[0] = changed_top_dict[pl.bottom[0]]
                # update "change" dict
                changed_top_dict[pl.top[0]] = pl.name
                top_change_before_dict[pl.name] = pl.top[0]
                # change current layer's output to its name
                pl.top[0] = pl.name
            else:
                if pl.bottom[0] in changed_top_dict:
                    pl.bottom[0] = changed_top_dict[pl.bottom[0]]
        # if the layer does not
        else:
            for index, plt in enumerate(pl.bottom):
                if plt in changed_top_dict:
                    pl.bottom[index] = changed_top_dict[plt]
    return top_change_before_dict


def _get_inputs_outputs(predict_layer):
    """Obtain Caffe model's inputs and outpus"""
    # model inputs / outputs
    model_inputs = list()
    model_outputs = list()

    # The bottoms of every layer can not be as outputs
    not_outputs = set()
    for pl in predict_layer:
        if pl.type == "Input":
            assert len(pl.top) == 1, "The number of Input layer's output is more than 1."
            model_inputs.append(pl.top[0])
        for i in pl.bottom:
            not_outputs.add(i)

    for pl in predict_layer:
        if len(pl.bottom) > 0:
            for t in pl.top:
                if t not in not_outputs:
                    model_outputs.append(t)
    return model_inputs, model_outputs


def from_caffe(init_net, predict_net, shape_dict, dtype_dict):
    """Convert from caffe model into compatible relay Function.

    Parameters
    ----------
    init_net : caffe_pb2.NetParameter
        caffemodel
    predict_net : caffe_pb2.NetParameter
        caffe prototxt
    shape_dict : dict of str to int list/tuple
        Input shapes of the model.
    dtype_dict : dict of str to str
        Input types of the model.

    Returns
    -------
    mod : tvm.IRModule
        The relay module for compilation.

    params : dict of str to tvm.NDArray
        The parameter dict to be used by relay
    """
    logging.debug("caffe frontend")

    old_caffe = False
    if len(predict_net.input) != 0:  # old caffe version
        old_caffe = True
        model_inputs = list(predict_net.input)

    predict_layer = predict_net.layer

    # replace layer's top with its name and update other layers'bottoms
    top_change_before_dict = _rebuild_layers(predict_layer)

    # obtain inputs and outputs of Net
    if old_caffe:
        _, model_outputs = _get_inputs_outputs(predict_layer)
    else:
        model_inputs, model_outputs = _get_inputs_outputs(predict_layer)

    logging.debug("model_inputs:%s", ",".join(model_inputs))
    logging.debug("model_outputs:%s", ",".join(model_outputs))

    exp_tab = ExprTable()
    for in_name in model_inputs:
        shape = shape_dict[in_name] if in_name in shape_dict else None
        dtype = dtype_dict[in_name] if in_name in dtype_dict else "float32"
        exp_tab.set_expr(in_name, _expr.var(in_name, shape=shape, dtype=dtype))

    if list(init_net.layer):
        init_layer = init_net.layer
    else:
        init_layer = init_net.layers
    init_layer_dict = {il.name: il for il in init_layer}
    predict_layer_dict = {pl.name: pl for pl in predict_layer}
    # op code in model
    op_converter = OperatorConverter(init_layer_dict, predict_layer, exp_tab)
    op_converter.check_unsupported_ops()
    op_converter.op_fuse()
    op_converter.convert_op_to_relay()

    # params and outputs
    params = {k: _nd.array(np.array(v)) for k, v in exp_tab.params.items()}
    outputs = list()
    for n in model_outputs:
        if n in op_converter.changed_layers:
            n = op_converter.changed_layers[n]
        outputs.append(exp_tab.get_expr(n))
    outputs = outputs[0] if len(outputs) == 1 else _expr.Tuple(outputs)
    func = _function.Function(analysis.free_vars(outputs), outputs)
    mod = IRModule.from_expr(func)

    # return mod, params
    new_outputs = list()
    for i in model_outputs:
        if i in top_change_before_dict:
            tmp = top_change_before_dict[i]
        else:
            tmp = i
        new_outputs.append(tmp)
    return mod, params, new_outputs
