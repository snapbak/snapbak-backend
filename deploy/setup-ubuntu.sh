#!/usr/bin/env bash
# Sets up Snapbak Cloud on an Ubuntu EC2 box (docs/deploy.md): Redis, ffmpeg, the pinned tools, the venv and the two
# services. Run it from the repo root as the user the services run as; run it again after a pull. Safe to repeat.
set -euo pipefail

dir=$(pwd)
user=$(id -un)
shims="$HOME/.local/share/mise/shims"

sudo apt-get update
# ffmpeg: the worker cuts voiceprint clips. nodejs: pyright (`just lint`); its own Node download breaks.
sudo apt-get install -y redis-server ffmpeg nodejs curl git
sudo systemctl enable --now redis-server

command -v mise >/dev/null || curl -fsSL https://mise.run | sh
export PATH="$shims:$HOME/.local/bin:$PATH"
# So `just` works in a login shell on the box too.
grep -q 'mise activate' ~/.bashrc || echo 'eval "$(~/.local/bin/mise activate bash)"' >> ~/.bashrc
mise trust
mise install
uv sync --frozen

if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo "Fill in .env (docs/deploy.md), then run deploy/setup-ubuntu.sh again."
    exit 1
fi
chmod 600 .env

# The units name just by its absolute path: systemd never searches PATH, and a bad path fails as `/just` (203/EXEC).
just=$(command -v just)
[ -x "$just" ] || { echo "just not found under $shims; run mise install"; exit 1; }
for unit in snapbak-server snapbak-worker; do
    sed -e "s|/home/ubuntu/snapbak-backend|$dir|g" \
        -e "s|/home/ubuntu/.local/share/mise/shims/just|$just|g" \
        -e "s|/home/ubuntu/.local/share/mise/shims|$shims|g" \
        -e "s|/home/ubuntu/.local/bin|$HOME/.local/bin|g" \
        -e "s|^User=ubuntu$|User=$user|" \
        "deploy/$unit.service" | sudo tee "/etc/systemd/system/$unit.service" >/dev/null
    for bin in $(sed -n 's/^ExecStart[a-zA-Z]*=\([^ ]*\).*/\1/p' "/etc/systemd/system/$unit.service"); do
        [ -x "$bin" ] || { echo "$unit: $bin is not an executable"; exit 1; }
    done
done
sudo systemctl daemon-reload
sudo systemctl enable snapbak-server snapbak-worker
sudo systemctl restart snapbak-server snapbak-worker
systemctl --no-pager status snapbak-server snapbak-worker
