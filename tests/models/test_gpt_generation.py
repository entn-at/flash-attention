import re

import torch
import pytest

from einops import rearrange

from transformers import GPT2Config, GPT2Tokenizer
from transformers.models.gpt2.modeling_gpt2 import GPT2LMHeadModel as GPT2LMHeadModelHF

from flash_attn.models.gpt import GPTLMHeadModel
from flash_attn.models.gpt import remap_state_dict_gpt2
from flash_attn.utils.pretrained import state_dict_from_pretrained


@pytest.mark.parametrize('fused_ft_kernel', [False, True])
# @pytest.mark.parametrize('fused_ft_kernel', [True])
@pytest.mark.parametrize('optimized', [False, True])
# @pytest.mark.parametrize('optimized', [False])
@pytest.mark.parametrize('rotary', [False, True])
# @pytest.mark.parametrize('rotary', [False])
@pytest.mark.parametrize('model_name', ["gpt2"])
def test_greedy_decode(model_name, rotary, optimized, fused_ft_kernel):
    """Check that our implementation of GPT2 generation matches the HF implementation:
    the scores in fp16 should be around the same as the HF scores in fp16, when compared to
    the HF scores in fp32.
    """
    dtype = torch.float16
    device = 'cuda'
    rtol, atol = 3e-3, 3e-1
    config = GPT2Config.from_pretrained(model_name)
    if rotary:
        config.n_positions = 0
        config.rotary_emb_dim = 64
    if optimized:
        config.use_flash_attn = True
        config.fused_bias_fc = True
        config.fused_dense_gelu_dense = True
        config.fused_dropout_add_ln = True

    # if not rotary, we load the weight from HF but ignore the position embeddings.
    # The model would be nonsense but it doesn't matter for the test.
    model = GPTLMHeadModel.from_pretrained(model_name, config, strict=not rotary, device=device)
    model = model.to(dtype=dtype)
    model.eval()

    if not rotary:
        model_ref = GPT2LMHeadModelHF.from_pretrained(model_name).cuda()
        model_hf = GPT2LMHeadModelHF.from_pretrained(model_name).cuda().to(dtype=dtype)
        model_ref.eval()
        model_hf.eval()

    torch.manual_seed(0)
    tokenizer = GPT2Tokenizer.from_pretrained("gpt2")
    input_ids = tokenizer("Hello, my dog is cute and ", return_tensors="pt").input_ids.cuda()
    max_length = 30
    # input_ids = torch.randint(0, 100, (1, 10), dtype=torch.long, device='cuda')
    # max_length = input_ids.shape[1] + 40

    # Slow generation for reference
    sequences = []
    scores = []
    cur_input_ids = input_ids
    with torch.inference_mode():
        scores.append(model(cur_input_ids).logits[:, -1])
        sequences.append(scores[-1].argmax(dim=-1))
        for _ in range(input_ids.shape[1] + 1, max_length):
            cur_input_ids = torch.cat([cur_input_ids, rearrange(sequences[-1], 'b -> b 1')], dim=-1)
            scores.append(model(cur_input_ids).logits[:, -1])
            sequences.append(scores[-1].argmax(dim=-1))
    sequences = torch.cat([input_ids, torch.stack(sequences, dim=1)], dim=1)
    scores = tuple(scores)

    out = model.generate(input_ids=input_ids, max_length=max_length,
                         fused_ft_kernel=fused_ft_kernel,
                         return_dict_in_generate=True, output_scores=True, timing=True)
    print(out.sequences)
    if fused_ft_kernel:
        out_cg = model.generate(input_ids=input_ids, max_length=max_length,
                                fused_ft_kernel=fused_ft_kernel, cg=True,
                                return_dict_in_generate=True, output_scores=True, timing=True)
        print(out_cg.sequences)

    if not rotary:
        out_hf = model_hf.generate(input_ids=input_ids, max_length=max_length,
                                return_dict_in_generate=True, output_scores=True)
        out_ref = model_ref.generate(input_ids=input_ids, max_length=max_length,
                                    return_dict_in_generate=True, output_scores=True)

        print(f'Scores max diff: {(torch.stack(out.scores, 1) - torch.stack(out_ref.scores, 1)).abs().max().item()}')
        print(f'Scores mean diff: {(torch.stack(out.scores, 1) - torch.stack(out_ref.scores, 1)).abs().mean().item()}')
        print(f'HF fp16 max diff: {(torch.stack(out_hf.scores, 1) - torch.stack(out_ref.scores, 1)).abs().max().item()}')
        print(f'HF fp16 mean diff: {(torch.stack(out_hf.scores, 1) - torch.stack(out_ref.scores, 1)).abs().mean().item()}')

    assert torch.all(out.sequences == sequences)
    assert torch.allclose(torch.stack(out.scores, dim=1), torch.stack(scores, dim=1),
                          rtol=rtol, atol=atol)
    if not rotary:
        assert torch.all(out.sequences == out_ref.sequences)
        assert torch.all(out.sequences == out_hf.sequences)

        assert (torch.stack(out.scores, 1) - torch.stack(out_ref.scores, 1)).abs().max().item() < 3 * (torch.stack(out_hf.scores, 1) - torch.stack(out_ref.scores, 1)).abs().max().item()
