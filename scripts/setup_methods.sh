#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

METHODS_DIR="${MISEVOLVE_METHODS_DIR:-external/methods}"
mkdir -p "$METHODS_DIR"

install_method() {
  local name="$1"
  local url="$2"
  local commit="$3"
  local target="$METHODS_DIR/$name"
  if [ -d "$target/.git" ]; then
    git -C "$target" fetch origin "$commit"
  elif [ -e "$target" ]; then
    echo "target exists and is not a Git checkout: $target" >&2
    return 2
  else
    git clone --no-checkout "$url" "$target"
  fi
  git -C "$target" checkout --detach "$commit"
}

install_method AutoSkill https://github.com/ECNU-ICALK/AutoSkill.git 94c47ca488d4ba4117d20272e66d49b9877e68cf
install_method EvoSkill https://github.com/sentient-agi/EvoSkill.git 36f6f04952293d7054145550c2b9f0b0411bff1c
install_method SkillClaw https://github.com/AMAP-ML/SkillClaw.git bf4dc2ee9430ecffb60e19630d26f57dfa2bd326
install_method SkillOpt https://github.com/microsoft/SkillOpt.git 57333f3406436a90a2b5feec4aad74ddb33d6e85
install_method skills-vote https://github.com/MemTensor/skills-vote.git 86fd73915e9ea4b594ca0edbd8c3267109a8f976

if [ "${MISEVOLVE_SKIP_METHOD_INSTALL:-0}" != "1" ]; then
  command -v uv >/dev/null 2>&1 || { echo "uv is required to create isolated method environments" >&2; exit 2; }
  install_environment() {
    local name="$1"
    local source="$2"
    local python=".venvs/$name/bin/python"
    uv venv --python 3.12 ".venvs/$name"
    uv pip install --python "$python" -e . -e "$source"
  }
  install_environment autoskill "$METHODS_DIR/AutoSkill"
  install_environment evoskill "$METHODS_DIR/EvoSkill"
  install_environment skillclaw "$METHODS_DIR/SkillClaw[evolve]"
  install_environment skillopt "$METHODS_DIR/SkillOpt"
  install_environment skillsvote "$METHODS_DIR/skills-vote"
fi
