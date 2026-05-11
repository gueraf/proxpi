# uv-fast

A thin wrapper around [`uv`](https://github.com/astral-sh/uv) that routes traffic through a local [proxpi](https://github.com/gueraf/proxpi) instance when one is running, and otherwise hands off to `uv` unchanged.

Useful when your direct path to PyPI/Fastly is slow per-flow but a parallel-range-request proxy can saturate the link.

## Install

```bash
pipx install git+https://github.com/gueraf/proxpi.git#subdirectory=contrib/uv-fast
```

Or from a local clone:

```bash
pipx install /path/to/proxpi/contrib/uv-fast
```

## Use

Anywhere you'd call `uv`, call `uv-fast` instead:

```bash
uv-fast sync
uv-fast pip install foo
```

If proxpi is reachable at `http://127.0.0.1:5000` it gets used. If not, the call is identical to a plain `uv` invocation.

## Configuration

| Env var | Default | Description |
|---|---|---|
| `UV_FAST_PROXPI` | `http://127.0.0.1:5000` | Base URL of the local proxpi |
| `UV_FAST_TIMEOUT` | `0.3` | Health-probe timeout (seconds) before falling back |
