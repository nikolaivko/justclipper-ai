#!/bin/bash
set -euo pipefail
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd -P )"
cd "$DIR"
if [[ "$(uname -s)" != "Darwin" ]] || [[ ! -x /usr/bin/sandbox-exec ]]; then
    echo "Contained local execution requires the existing macOS sandbox-exec tool." >&2
    exit 1
fi
export TMPDIR="$DIR/data/temp"
export TMP="$TMPDIR"
export TEMP="$TMPDIR"
export XDG_CACHE_HOME="$DIR/.cache"
export XDG_CONFIG_HOME="$DIR/data/config"
export XDG_DATA_HOME="$DIR/data/local/share"
export HF_HOME="$DIR/.cache/huggingface"
export TORCH_HOME="$DIR/.cache/torch"
export PIP_CACHE_DIR="$DIR/.cache/pip"
export DENO_DIR="$DIR/.cache/deno"
export CLANG_MODULE_CACHE_PATH="$DIR/.cache/clang"
export CCACHE_DIR="$DIR/.cache/ccache"
export PYTHONPYCACHEPREFIX="$DIR/.cache/python"
# Child-process lookup only; never alter the user's shell or system PATH.
export PATH="$DIR/bin:$PATH"
exec /usr/bin/sandbox-exec -D "PROJECT_ROOT=$DIR" -f "$DIR/scripts/project-local.sb" "$@"
