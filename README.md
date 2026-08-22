# fantasy-gm

AI-assisted ESPN fantasy football manager.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
# Fill ESPN_SWID and ESPN_S2 in .env
```

## Commands

```bash
fantasy-gm league
fantasy-gm players --limit 50
fantasy-gm draft status
fantasy-gm draft watch
```

`draft watch` is read-only. It polls ESPN draft state and prints each newly recorded pick.

