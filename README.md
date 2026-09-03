# any-agent

Drive local coding agents (**OpenCode**, **Claude Code**) from **WeChat** on your phone.

One machine — the **hub** — owns the single WeChat connection. Other machines join as
**devices** over their own [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
tunnels and register their agents with the hub. From your phone you pick any agent on any
machine with `/use <device>/<agent>`, and the hub relays prompts and replies — including
images and files.

```
                         ┌───────────────── hub machine ──────────────────┐
 iPhone WeChat  ──────▶  │  any-agent (hub)  ──▶  local opencode / claude │
                         │        ▲   │                                   │
                         └────────│───┼───────────────────────────────────┘
                                  │   │  cloudflared tunnels (JSON over HTTPS)
                     register /   │   ▼  prompt / result / media / logs
                     heartbeat  ┌─┴───────────── device "box1" ────────────┐
                                │  any-agent (device) ──▶ opencode/claude  │
                                └──────────────────────────────────────────┘
```

## Requirements

- Python 3.10+
- [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
  — only needed for multi-machine (hub/device) setups. **Auto-installed on first run**
  (Homebrew on macOS, or a direct binary download) if not already on your PATH.
- At least one coding-agent CLI on the machine that runs it:
  [`opencode`](https://github.com/sst/opencode) and/or
  [`claude`](https://docs.anthropic.com/en/docs/claude-code) (Claude Code).

## Install

Build the wheel and install it:

```bash
python3 -m pip install build
python3 -m build --wheel
python3 -m pip install dist/any_agent-*.whl
```

Or install straight from the source tree:

```bash
python3 -m pip install .
```

This installs the `any-agent` command and a runnable module (`python3 -m any_agent`),
and bundles the `send-file` skill so agents can send you images/files.

## Quick start (single machine)

```bash
# 1. First run — writes a starter config and installs the send-file skill, then stops.
python3 -m any_agent

# 2. Edit any_agent_config.yaml (set each agent's cwd, pick models — see below).

# 3. Log in to WeChat (QR code prints in the terminal; scan it once).
python3 -m any_agent --login

```
<img src=imgs/wechat.png />

<img src=imgs/qrcode.png />

```bash


# 4. Later runs reuse the saved login — no QR, no --login needed.
python3 -m any_agent
```

What the first run does automatically:
1. Installs the bundled `send-file` skill into `./.claude/skills/` and `./.opencode/skills/`
   (idempotent — never overwrites your files).
2. Writes a starter `any_agent_config.yaml` **only if one doesn't exist** (an existing
   config is never touched).
3. With `--login`, walks you through WeChat QR login. Credentials are saved to
   `~/.any_agent/credentials.json` and reused on every later run. Only `--login` forces
   a fresh login; nothing else discards your saved credentials.

## WeChat commands

Send these as chat messages to your bot:

| Command | What it does |
|---|---|
| `/devices` | List all agents — local and remote — with the current one marked `*` |
| `/use <agent>` | Switch to a local agent (e.g. `/use claude`) |
| `/use <device>/<agent>` | Switch to a remote agent (e.g. `/use box1/opencode`) |
| `/cwd [path]` | Show or change the working directory |
| `/model [id]` | Show current model, or switch it (e.g. `/model claude-opus-4-6`) |
| `/models` | List available models |
| `/sessions` | List past sessions as a numbered menu |
| `/resume <n>` | Resume session number `n` from the last `/sessions` list |
| `/new` | Start a fresh session |
| `/status` | Current agent, directory, and online devices |
| `/skills` | List the agent's available skills |
| `/mcps` | List MCP servers |
| `/help` | Show the command list |

Anything not starting with `/` is sent to the current agent as a prompt.

### Sessions on mobile — no UUIDs to type

`/sessions` shows a numbered menu; you resume by number:

```
Sessions for opencode (wx_plugin):
  1. Fix media send bug      2m ago  *
  2. Refactor acp modules    3h ago
  3. Add hub routing         1d ago
  … 5 older
(reply /resume <n>)
```

Then just send `/resume 2`. Sessions are scoped to the current directory, so `/cwd`
changes which sessions you see.

### Sending files back to you

Ask an agent to "create a chart and send it to me". The bundled `send-file` skill tells
the agent to emit a `sendfile://` marker; the hub uploads the file to WeChat and delivers
it as an image/video/document. Works for local and remote agents.

## Adding another machine to the hub

Any number of extra machines can join the hub as **devices**. Each device runs the same
`any-agent` package in `device` mode, exposes its own cloudflared tunnel, and registers
its local agents with the hub. The hub then lists them as `<device>/<agent>`.

### 1. On the hub — note its public URL

When the hub starts it prints its tunnel URL:

```
Hub reachable at https://marion-third-genres.trycloudflare.com (share this with devices)
```

Copy that URL. (The hub is just `python3 -m any_agent --login` as above; `mode: hub`
is the default.)

### 2. On the device machine — install and run in device mode

```bash
python3 -m pip install dist/any_agent-*.whl     # same wheel
python3 -m any_agent \
  --mode device \
  --hub-url https://marion-third-genres.trycloudflare.com \
  --device-name box1
```

The device:
- installs the `send-file` skill and writes its own starter config on first run,
- starts its own cloudflared tunnel,
- registers its agents with the hub and sends a heartbeat every 60s.

No WeChat login is needed on a device — only the hub talks to WeChat.

You can also put these in the device's `any_agent_config.yaml` instead of flags:

```yaml
mode: device
device_name: box1
hub_url: "https://marion-third-genres.trycloudflare.com"
http_port: 8787
```

### 3. From WeChat

```
/devices                 → shows  box1/opencode, box1/claude, ...
/use box1/claude         → route your prompts to Claude on box1
```

### Failover

If a device stops sending heartbeats (~90s), the hub messages you
("Device 'box1' went offline. Switched you to 'opencode'.") and moves you back to the
default agent. When the device comes back it re-registers automatically.

### Notes

- **Each device needs its own tunnel** — that's how the hub reaches it. `cloudflared` is
  auto-installed on the device too.
- If you run a device on the **same machine** as the hub, give it a different `http_port`
  (e.g. `8788`) so the two servers don't collide.
- There is **no authentication** between hub and devices yet — only share the hub URL with
  machines you trust.

## Configuration

`any_agent_config.yaml` (auto-generated on first run, then yours to edit):

```yaml
ilink:
  base_url: "https://ilinkai.weixin.qq.com"
  cdn_base_url: "https://novac2c.cdn.weixin.qq.com/c2c"

mode: hub                 # "hub" or "device"
device_name: hub          # this node's name, used in <device>/<agent>
hub_url: ""               # device mode only: the hub's public URL
http_port: 8787           # local port the cloudflared tunnel points at

default_agent: opencode   # agent for new users / before any /use

devices:                  # the LOCAL agents THIS machine hosts
  - name: opencode
    type: opencode
    bin: opencode
    cwd: /path/to/your/project
    args: ["acp"]
    model: deepseek/deepseek-v4-pro

  - name: claude
    type: claude
    bin: claude
    cwd: /path/to/your/project
    args: []
    model: claude-opus-4-6
```

Per-agent fields:

| Field | Meaning |
|---|---|
| `name` | Label used in WeChat (`/use <name>`) |
| `type` | `opencode` (native ACP) or `claude` (Claude Code CLI) |
| `bin` | Path/name of the agent binary |
| `cwd` | Working directory the agent runs in (also scopes its session list) |
| `args` | opencode: CLI args (`["acp"]`); claude: allowed-tools list (usually `[]`) |
| `model` | Default model — see below |

## Choosing a model

`model` in the config sets the default; `/model <id>` switches it at runtime, and
`/models` lists what's available. The **format differs per agent**:

### OpenCode — `provider/model`

OpenCode models are written as `<provider>/<model>`:

```yaml
model: deepseek/deepseek-v4-pro
model: anthropic/claude-sonnet-4-6
model: openai/gpt-4o
```

Where to find the names:
- Run `/models` in WeChat once an opencode session is active — it lists every model the
  server knows, in the exact `provider/model` form to paste into `/model`.
- Or run `opencode models` in a terminal on that machine.
- Providers/keys come from your OpenCode config (`~/.config/opencode/`); any provider you've
  configured there shows up.

### Claude Code — model id or alias

Claude uses a bare model id (no provider prefix), passed as `--model`:

```yaml
model: claude-opus-4-6
model: claude-sonnet-4-6
```

Where to find the names:
- Run `/model` in WeChat (Claude has no separate list; `/models` is treated the same as
  `/model`) — it shows the current model.
- Or run `claude --help` / see the Claude Code docs for supported ids and short aliases
  (e.g. `opus`, `sonnet`).

Leave `model` blank to use each agent's own default.

## Runtime state & files

| Path | What |
|---|---|
| `~/.any_agent/credentials.json` | Saved WeChat login (reused across runs) |
| `~/.any_agent/combined.jsonl` | Hub's merged log, including remote device logs |
| `./any_agent_config.yaml` | Your config (generated once, never overwritten) |
| `./.claude/skills/`, `./.opencode/skills/` | Bundled skills installed on first run |

## Troubleshooting

- **"cloudflared not found" / install fails** — install it manually
  (`brew install cloudflared` or from the Cloudflare downloads page) and re-run.
- **Port already in use (8787)** — another node is using it; change `http_port`.
- **Remote agent shows offline** — check the device process is running and its
  `--hub-url` matches the hub's current tunnel URL (the URL changes each hub restart
  unless you use a named tunnel).
- **Skip first-run skill install** — pass `--no-bootstrap`.

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests/      # full suite
python main.py --login       # run from the source tree
```

