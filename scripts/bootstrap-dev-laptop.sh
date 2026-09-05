#!/bin/bash
# One-time setup for a new developer laptop (Option B). Run this once when
# joining the project, not before every dev-start.sh -- checks for and
# installs everything dev-start.sh/Git Console need, explaining each step
# rather than silently doing things, since the whole point of this tooling
# is reducing "what is this doing to my machine" anxiety, not adding to it.
#
# Assumes an apt-based Linux (Ubuntu/Debian) -- matches every real
# developer laptop this has actually been run on so far. If that's not
# your setup, this tells you clearly and exits rather than fail
# confusingly partway through.
#
# Safe to re-run: every check is idempotent, skips whatever's already
# satisfied. Never embeds a secret -- AWS credentials are entered
# interactively into `aws configure`, never accepted as an argument or
# written into this script; your GitHub key is generated locally and only
# ever asks you to copy out the PUBLIC half.
#
# Per-client module versioning (docs/PER_CLIENT_MODULE_VERSIONING.md):
# every real client now has its own dedicated repo -- there is no shared
# addons repo left to clone. The client list below is read straight out
# of clients.yaml at run time, not hardcoded, so a 6th/7th client added
# later needs zero changes here.
#
# Usage: ./bootstrap-dev-laptop.sh [--repo-dir /path/to/put/repos]
set -euo pipefail

REPO_DIR="${1:-$HOME}"
if [ "${1:-}" = "--repo-dir" ]; then
    REPO_DIR="${2:?--repo-dir needs a path}"
fi

BUILD_DIR="$REPO_DIR/DevOps_Files"
CLIENTS_YAML="$BUILD_DIR/clients.yaml"
S3_BUCKET="orion-instruments-erp16-bucket"
AWS_REGION="ap-south-1"

say() { echo ""; echo "=== $1 ==="; }
ok() { echo "  OK: $1"; }
doing() { echo "  -> $1"; }
fail() { echo "  ERROR: $1" >&2; exit 1; }

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
doing "installing pyyaml, jinja2, flask (needed by render_client.py and Git Console)"
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

# `ssh -T git@github.com` exits non-zero even when authentication genuinely
# succeeds (GitHub's own documented behavior for that command, since it
# never grants shell access) -- with `pipefail` active, piping it straight
# into `grep -q` would silently discard a real match. Capture the output
# first instead, so a successful pipeline (`echo` always exits 0) is all
# `pipefail` ever sees. Also explicitly detached from stdin (`</dev/null`)
# -- without that, ssh silently inherits and can consume from the same
# pipe a later `read -rp` in this script needs, even though ssh itself
# never actually needs any input for this command. Found live: piping a
# profile name in for the AWS prompt further down was getting eaten here
# first, making `read` hit EOF and abort the whole script under `set -e`.
github_ssh_ok() {
    local out
    out="$(ssh -T git@github.com -o BatchMode=yes -o ConnectTimeout=6 </dev/null 2>&1 || true)"
    echo "$out" | grep -q "successfully authenticated"
}

say "GitHub SSH access"
# Never generated on your behalf if you already have working access -- the
# whole point of an SSH keypair is that the private half never needs to
# leave the machine it was created on.
if github_ssh_ok; then
    ok "GitHub SSH access already works with your current default key"
else
    mkdir -p "$HOME/.ssh"
    chmod 700 "$HOME/.ssh"
    KEY_PATH="$HOME/.ssh/erp16_github"
    if [ -f "$KEY_PATH" ]; then
        ok "found an existing $KEY_PATH -- reusing it"
    else
        doing "no working GitHub SSH access found -- generating a new dedicated key (no passphrase needed for this one; it's already scoped down to just your assigned repos, unlike the admin/production keys)"
        ssh-keygen -t ed25519 -C "$(whoami)-erp16-dev" -f "$KEY_PATH" -N ""
    fi
    if ! grep -q "^Host github.com$" "$HOME/.ssh/config" 2>/dev/null; then
        doing "pointing github.com at this key in ~/.ssh/config"
        mkdir -p "$HOME/.ssh"
        {
            echo ""
            echo "Host github.com"
            echo "    HostName github.com"
            echo "    User git"
            echo "    IdentityFile $KEY_PATH"
            echo "    IdentitiesOnly yes"
        } >> "$HOME/.ssh/config"
        chmod 600 "$HOME/.ssh/config"
    fi
    echo ""
    echo "  Add this PUBLIC key to your GitHub account (https://github.com/settings/keys):"
    echo "  -----------------------------------------------------------------"
    cat "${KEY_PATH}.pub"
    echo "  -----------------------------------------------------------------"
    read -rp "  Press Enter once you've added it, and admin has added your GitHub account as a collaborator on your assigned repos..."
    github_ssh_ok \
        || fail "GitHub still not reachable with this key -- double-check the public key was added, then re-run this script."
    ok "GitHub SSH access confirmed"
fi

say "Repositories"
# On a genuinely fresh machine, github.com isn't in known_hosts yet -- the
# first SSH connection (from any of the clones below) would otherwise
# prompt to confirm its host key, which hits the exact same stdin-theft
# problem as the ssh call above. accept-new trusts it automatically (same
# key GitHub has published for years) without ever touching this script's
# stdin.
export GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new"
if [ -d "$BUILD_DIR/.git" ]; then
    ok "DevOps_Files already cloned at $BUILD_DIR"
else
    doing "cloning DevOps_Files into $BUILD_DIR"
    git clone git@github.com:kuOrion/DevOps_Files.git "$BUILD_DIR"
fi

doing "reading the current client list from clients.yaml (not hardcoded -- picks up new clients automatically)"
CLIENT_REPOS="$(python3 - "$CLIENTS_YAML" <<'PY'
import sys, yaml
with open(sys.argv[1]) as f:
    data = yaml.safe_load(f)["clients"]
for cid, cfg in data.items():
    repo = cfg.get("git_repo")
    if repo:
        print(f"{cid} {repo}")
PY
)"
[ -n "$CLIENT_REPOS" ] || fail "clients.yaml has no clients with git_repo set -- check the file, something's wrong."

while read -r CLIENT_ID REPO; do
    [ -z "$CLIENT_ID" ] && continue
    TARGET="$REPO_DIR/erp16-$CLIENT_ID"
    if [ -d "$TARGET/.git" ]; then
        ok "$CLIENT_ID already cloned at $TARGET"
    else
        doing "cloning $CLIENT_ID ($REPO) into $TARGET"
        git clone "git@github.com:${REPO}.git" "$TARGET"
    fi
done <<< "$CLIENT_REPOS"

say "AWS credentials"
read -rp "  AWS profile name to use [default: $(whoami)-dev-prod]: " AWS_PROFILE
AWS_PROFILE="${AWS_PROFILE:-$(whoami)-dev-prod}"

if aws sts get-caller-identity --profile "$AWS_PROFILE" >/dev/null 2>&1; then
    ok "AWS profile '$AWS_PROFILE' already configured and working"
else
    echo "  You need a scoped AWS credential (read-only on sanitized snapshots) to"
    echo "  pull dev data. Get this from admin over a secure channel -- never as"
    echo "  plaintext in chat -- then enter it below (region: $AWS_REGION)."
    echo ""
    aws configure --profile "$AWS_PROFILE"
fi

doing "verifying this profile can actually read sanitized snapshots"
aws s3 ls "s3://$S3_BUCKET/sanitized/" --profile "$AWS_PROFILE" >/dev/null 2>&1 \
    || fail "could not list sanitized/ with profile '$AWS_PROFILE' -- check the credentials, or ask admin to confirm the IAM grant."
ok "access confirmed -- can read sanitized/ as expected"

say "Convenience launcher"
LAUNCHER="$BUILD_DIR/start-git-console.sh"
cat > "$LAUNCHER" <<EOF
#!/bin/bash
# Generated by bootstrap-dev-laptop.sh -- bakes in your AWS profile/bucket
# so you don't need to remember or retype them every time.
exec env DEV_CONSOLE_AWS_PROFILE="$AWS_PROFILE" DEV_CONSOLE_S3_BUCKET="$S3_BUCKET" \\
    python3 "$BUILD_DIR/scripts/git_console/app.py"
EOF
chmod +x "$LAUNCHER"
ok "wrote $LAUNCHER"

say "Shell shortcut"
# No SSH tunnel needed here, unlike Admin Console -- Git Console runs
# entirely locally (your own Docker, your own scoped S3 read, GitHub
# directly) and never talks to production at all. So this just needs to
# start it if it isn't already running, then open the browser.
MARKER="# ERP16 opengitconsole (added by bootstrap-dev-laptop.sh)"
if grep -qF "$MARKER" "$HOME/.bashrc" 2>/dev/null; then
    ok "opengitconsole already in ~/.bashrc"
else
    doing "adding 'opengitconsole' shell function to ~/.bashrc"
    cat >> "$HOME/.bashrc" <<EOF

$MARKER
opengitconsole() {
    local port=5151
    local launcher="$LAUNCHER"
    if ! curl -s -o /dev/null "http://127.0.0.1:\$port/" 2>/dev/null; then
        if [ ! -x "\$launcher" ]; then
            echo "Git Console launcher not found at \$launcher -- run bootstrap-dev-laptop.sh first."
            return 1
        fi
        echo "Starting Git Console..."
        setsid "\$launcher" > "\$HOME/.erp16_git_console.log" 2>&1 &
        disown
        local i
        for i in \$(seq 1 20); do
            curl -s -o /dev/null "http://127.0.0.1:\$port/" 2>/dev/null && break
            sleep 0.5
        done
    fi
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "http://127.0.0.1:\$port/" >/dev/null 2>&1 &
    else
        echo "Open this in your browser: http://127.0.0.1:\$port/"
    fi
}
EOF
    ok "added -- run 'source ~/.bashrc' (or open a new terminal), then just type: opengitconsole"
fi

say "Done"
echo "  Start Git Console: opengitconsole  (after 'source ~/.bashrc' or a new terminal)"
echo "  Or directly: $BUILD_DIR/start-git-console.sh, then open http://127.0.0.1:5151"
