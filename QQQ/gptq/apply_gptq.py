import os
import torch
import torch.nn as nn
import transformers
from tqdm import tqdm
from QQQ.utils import (
    get_loaders,
    get_model_architecture,
    str2torch_device,
    find_layers,
    recurse_setattr,
    get_max_length,
    free_memory
)
from .models import get_gptq_model_func
from .qlinear import dynamically_import_QuantLinear


@torch.no_grad()
def apply_gptq(model, q_config, args):
    gptq_config = q_config.gptq
    gptq_config.seqlen = get_max_length(model)
    dataloader, _ = get_loaders(
        gptq_config.dataset,
        nsamples=gptq_config.nsamples,
        seed=args.seed,
        model=args.tokenizer_path,
        seqlen=gptq_config.seqlen,
    )
    model_type = get_model_architecture(model.config)
    gptq_func = get_gptq_model_func(model_type)
    device = str2torch_device(args.device)

    quantizers = gptq_func(model, dataloader, device, gptq_config)
    torch.save(quantizers, os.path.join(args.save_path, "quantizers.pth"))
    model_save_path = os.path.join(args.save_path, 'after_gptq')
    os.makedirs(model_save_path, exist_ok=True)
    model.save_pretrained(model_save_path)

    pack_model(
        model,
        quantizers,
        bits=gptq_config.wbits,
        group_size=gptq_config.groupsize,
        is_marlin_format=gptq_config.use_marlin,
    )
    free_memory()
    return model


@torch.no_grad()
def pack_model(
    model,
    quantizers,
    bits,
    group_size,
    force_layer_back_to_cpu: bool = False,
    is_marlin_format: bool = False,
):
    QuantLinear = dynamically_import_QuantLinear(
        bits=bits,
        disable_marlin=not is_marlin_format,
    )
    CPU = torch.device("cpu")
    if force_layer_back_to_cpu:
        model.to(CPU)

    # logger.info("Packing model...")
    layers = find_layers(model)
    layers = {n: layers[n] for n in quantizers}
    make_quant(
        model,
        quantizers,
        bits,
        group_size,
        use_marlin=is_marlin_format,
    )
    qlayers = find_layers(model, [QuantLinear])

    pbar = tqdm(qlayers.keys(), leave=True)
    for name in pbar:
        pbar.set_description(f"Packing {name}...", refresh=True)

        scale, zero, g_idx, int8_scale = quantizers[name]
        # so far can only pack layer on CPU
        layer_device = qlayers[name].device
        qlayers[name].to(CPU)
        layers[name], scale, zero, g_idx, int8_scale = (
            layers[name].to(CPU),
            scale.to(CPU),
            zero.to(CPU),
            g_idx.to(CPU),
            int8_scale.to(CPU) if int8_scale is not None else None,
        )
        if QuantLinear.QUANT_TYPE == "marlin":
            qlayers[name].pack(layers[name], scale, int8_scale)
        else:
            qlayers[name].pack(layers[name], scale, zero, g_idx, int8_scale)
        qlayers[name].to(layer_device)
    print("Model packed.")


def make_quant(
    module,
    names,
    bits,
    group_size,
    use_marlin: bool = False,
    trainable: bool = False,
):
    QuantLinear = dynamically_import_QuantLinear(
        bits=bits,
        disable_marlin=not use_marlin,
    )

    if isinstance(module, QuantLinear):
        return

    for name, submodule in module.named_modules():
        if name in names:
            ori_layer_device = next(submodule.parameters()).device

            if isinstance(submodule, nn.Linear):
                in_features = submodule.in_features
                out_features = submodule.out_features
            elif isinstance(submodule, nn.Conv2d):
                in_features = submodule.in_channels
                out_features = submodule.out_channels
            elif isinstance(submodule, transformers.pytorch_utils.Conv1D):
                in_features = submodule.weight.shape[0]
                out_features = submodule.weight.shape[1]
            bias = submodule.bias is not None
            new_layer = QuantLinear(
                bits,
                group_size,
                in_features,
                out_features,
                bias,
                trainable=trainable,
                weight_dtype=submodule.weight.dtype,
            )
            new_layer.device = ori_layer_device
            recurse_setattr(module, name, new_layer.to(ori_layer_device))
