#!/bin/bash
set -e
source /etc/ai-rig/gpu.env
source /etc/ai-rig/devin.env

export CUDA_VISIBLE_DEVICES="$CUDA_VISIBLE_DEVICES"
export CUDA_DEVICE_ORDER=PCI_BUS_ID

ARGS=(
  -m "$ROLE_MODEL_PATH"
  --reasoning-format none
  --jinja
  --temp "$ROLE_TEMP"
  --top-p "$ROLE_TOP_P"
  --repeat-penalty "$ROLE_REPEAT_PENALTY"
  --tensor-split "$TENSOR_SPLIT"
  -c "$ROLE_CTX_SIZE"
  -ngl 999
  --host 0.0.0.0
  --port "$ROLE_LLAMA_PORT"
  --parallel 1
)

# KV cache: turbo*/TCQ su beellama, q8_0 su mainline. Richiedono flash-attn.
[ -n "${ROLE_FLASH_ATTN:-}" ]   && ARGS+=(--flash-attn "$ROLE_FLASH_ATTN")
[ -n "${ROLE_CACHE_TYPE_K:-}" ] && ARGS+=(--cache-type-k "$ROLE_CACHE_TYPE_K")
[ -n "${ROLE_CACHE_TYPE_V:-}" ] && ARGS+=(--cache-type-v "$ROLE_CACHE_TYPE_V")

if [ -n "$ROLE_MMPROJ_PATH" ]; then
  ARGS+=(--mmproj "$ROLE_MMPROJ_PATH")
fi

# DFlash (solo beellama, e solo se hai un drafter GGUF per QUESTO modello target).
# Metti il drafter in /opt/models/<ruolo>/dflash-drafter.gguf e riparte da solo.
DRAFTER="/opt/models/${ROLE_NAME}/dflash-drafter.gguf"
if [ "${LLAMA_FLAVOR:-mainline}" = "beellama" ] && [ -f "$DRAFTER" ]; then
  ARGS+=(--spec-type dflash --spec-draft-model "$DRAFTER" --spec-draft-ngl all)
fi

if [ -n "$ROLE_EXTRA_ARGS" ]; then
  # shellcheck disable=SC2206
  EXTRA=($ROLE_EXTRA_ARGS)
  ARGS+=("${EXTRA[@]}")
fi

cd /opt/llama.cpp/build/bin
exec ./llama-server "${ARGS[@]}"
