#!/bin/bash
# One-time setup for a new developer laptop (Option B). Run this once when
# joining the project, not before every dev-start.sh -- checks for and
# installs everything dev-start.sh/the dev console need, explaining each
# step rather than silently doing things, since the whole point of this
# tooling is reducing "what is this doing to my machine" anxiety, not
# adding to it.
#
# Assumes an apt-based Linux (Ubuntu/Debian) -- matches the sandbox and
# every laptop this has actually been tested on. If that's not your setup,
# this will tell you clearly rather than fail confusingly partway through.
#
# Safe to re-run: every check is idempotent, skips whatever's already
# satisfied.
#
# Usage: ./bootstrap-dev-laptop.sh [--repo-dir /path/to/put/repos]
set -euo pipefail

REPO_DIR="${1:-$HOME}"
if [ "${1:-}" = "--repo-dir" ]; then
    REPO_DIR="${2:?--repo-dir needs a path}"
fi

say() { echo ""; echo "=== $1 ==="; }
ok() { echo "  OK: $1"; }
doing() { echo "  -> $1"; }

if ! command -v apt-get >/dev/null 2>&1; then
    echo "This script assumes an apt-based Linux (Ubuntu/Debian) and apt-get isn't"
    echo "available here. The rest of this script won't work on your OS as-is --"
    echo "ask in the team channel for the equivalent steps on your platform, or"
    echo "install these by hand: git, Docker + docker compose v2, python3 with"
    echo "pip (pyyaml, jinja2, flask), AWS CLI v2."
    exit 1
fi

say "git"
if command -v git >/dev/null 2>&1; then
    ok "git already installed ($(git --version))"
else
    doing "installing git (needs sudo)"
    sudo apt-get update -qq && sudo apt-get install -y git
    ok "git installed"
fi

say "Docker"
if command -v docker >/dev/null 2>&1; then
    ok "docker already installed ($(docker --version))"
else
    doing "installing Docker (needs sudo) -- this adds Docker's own apt repo"
    curl -fsSL https://get.docker.com | sudo sh
    doing "adding $(whoami) to the docker group (log out/in for this to take effect)"
    sudo usermod -aG docker "$(whoami)"
    ok "Docker installed"
fi

say "docker compose (v2 plugin)"
if docker compose version >/dev/null 2>&1; then
    ok "docker compose already installed ($(docker compose version))"
else
    doing "installing docker-compose-v2 (needs sudo) -- the plain 'docker' package doesn't include this"
    sudo apt-get update -qq && sudo apt-get install -y docker-compose-v2
    ok "docker compose installed"
fi

say "python3 + pip packages"
if ! command -v python3 >/dev/null 2>&1; then
    doing "installing python3 (needs sudo)"
    sudo apt-get update -qq && sudo apt-get install -y python3 python3-pip
fi
doing "installing pyyaml, jinja2, flask (needed by render_client.py and the dev console)"
python3 -m pip install --quiet --user pyyaml jinja2 flask
ok "python packages installed"

say "AWS CLI v2"
if command -v aws >/dev/null 2>&1; then
    ok "aws cli already installed ($(aws --version))"
else
    doing "installing AWS CLI v2 (needs sudo)"
    TMPDIR=$(mktemp -d)
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "$TMPDIR/awscliv2.zip"
    unzip -q "$TMPDIR/awscliv2.zip" -d "$TMPDIR"
    sudo "$TMPDIR/aws/install"
    rm -rf "$TMPDIR"
    ok "AWS CLI installed"
fi

say "Repositories"
if [ -d "$REPO_DIR/DevOps_Files/.git" ]; then
    ok "DevOps_Files already cloned at $REPO_DIR/DevOps_Files"
else
    doing "cloning DevOps_Files into $REPO_DIR/DevOps_Files"
    git clone https://github.com/cj78/DevOps_Files.git "$REPO_DIR/DevOps_Files"
fi
if [ -d "$REPO_DIR/erp16-custom-addons/.git" ]; then
    ok "erp16-custom-addons already cloned at $REPO_DIR/erp16-custom-addons"
else
    doing "cloning erp16-custom-addons into $REPO_DIR/erp16-custom-addons (sibling to DevOps_Files, matching what dev-start.sh expects)"
    git clone https://github.com/cj78/erp16-custom-addons.git "$REPO_DIR/erp16-custom-addons"
fi

say "AWS credentials -- the one thing this script can't do for you"
if aws sts get-caller-identity --profile erp16-sandbox >/dev/null 2>&1; then
    ok "AWS profile 'erp16-sandbox' already configured and working"
else
    echo "  You need a scoped AWS credential to pull sanitized snapshots and secrets."
    echo "  Ask whoever's running this project for one, then run:"
    echo ""
    echo "    aws configure --profile erp16-sandbox"
    echo ""
    echo "  This script can't generate that credential for you -- it has to come"
    echo "  from someone with access to create it."
fi

say "Done"
echo "  Next: cd $REPO_DIR/DevOps_Files && scripts/dev-start.sh orion_test"
echo "  Or launch the dev console: python3 scripts/dev_console/app.py"
