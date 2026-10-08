import time
from llama_cpp import Llama

MODEL_PATH = r"C:\Users\Administrator\Documents\coding-projects\MindBridge\models\qwen_gguf\Qwen2.5-1.5B-Instruct-Q4_K_M.gguf"

print("Loading model...", flush=True)
llm = Llama(
    model_path=MODEL_PATH,
    n_ctx=2048,
    n_threads=4,
    verbose=False,
)

SYSTEM_PROMPT = (
    "You are MindBridge, a warm, Rogerian listener. "
    "The person you are talking to may be experiencing depression. "
    "Reflect what they said back to them in their own words. "
    "Ask one open question. Do not give advice unless asked twice. "
    "Do not diagnose. Do not mention medication. "
    "Keep replies to 2-3 sentences. Be human, not clinical."
)

USER_MESSAGE = "i feel drained and i don't see the point anymore"

prompt = (
    f"<|im_start|>system\n{SYSTEM_PROMPT}<|im_end|>\n"
    f"<|im_start|>user\n{USER_MESSAGE}<|im_end|>\n"
    f"<|im_start|>assistant\n"
)

print("Generating...", flush=True)
t0 = time.time()
out = llm(
    prompt,
    max_tokens=120,
    temperature=0.7,
    top_p=0.9,
    repeat_penalty=1.15,
    stop=["<|im_end|>", "<|im_start|>"],
)
elapsed = time.time() - t0

reply = out["choices"][0]["text"].strip()
print(f"\n--- REPLY ({elapsed:.1f}s) ---")
print(reply)
print("--- END ---")