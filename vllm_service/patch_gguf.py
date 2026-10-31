"""Runs while the image is built: two fixes vLLM 0.19 needs to run Gemma 3 GGUF on a Radeon. A failed assert stops the build: better than a patch that quietly stopped applying.
1. Transformers 5 dropped Siglip's vision_model level, but the GGUF loader and vLLM's Gemma 3 model still use it, so none of the picture-reading weights could be matched
   ("Failed to map GGUF parameters (437)"). This puts the level back in the names at load time.
2. Reading a prompt runs a Triton kernel with fixed chunks of 128 x 64 tokens. Gemma's heads are 256 wide and GGUF means float32, so that needs 128 KB of the card's working memory and a Radeon has 64 KB
   ("Required: 131072, Hardware limit: 65536"). Smaller chunks change how the work is split up, not the result; vLLM already does this for models with unusual block sizes.
3. Past 2 lines at once (6 on small layers) a multiply switches from the MMVQ kernel to MMQ, which on this card costs about 2.5 s a step: 8 people at once took 250 s instead of 8.
   MMVQ is the kernel it already allows up to 8 / 16 lines on the other quantization types; 8 covers VLLM_PARALLEL, 1024 is more than any prompt chunk (a long prompt: 62 s instead of 72 s)."""
from pathlib import Path
import vllm.model_executor.layers.quantization.gguf as quant
import vllm.model_executor.model_loader.gguf_loader as loader
import vllm.v1.attention.ops.prefix_prefill as prefill
def patch(module, old: str, new: str) -> None:
    path = Path(module.__file__)
    source = path.read_text(encoding="utf-8")
    assert source.count(old) == 1, f"{path.name} changed: check whether this patch is still needed"
    path.write_text(source.replace(old, new), encoding="utf-8")
    print("patched", path)
line = "        state_dict = dummy_model.state_dict()\n"
patch(loader, line, line + '        state_dict = {re.sub(r"^model\\.vision_tower\\.(?!vision_model\\.)", "model.vision_tower.vision_model.", k): v for k, v in state_dict.items()}\n')
patch(prefill, "        BLOCK_M = 128\n        BLOCK_N = 64\n", "        BLOCK_M = 16\n        BLOCK_N = 16\n")
patch(quant, "        mmvq_safe = 2 if qweight.shape[0] > 5120 else 6\n", "        mmvq_safe = 8 if qweight.shape[0] > 5120 else 1024\n")
