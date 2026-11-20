import importlib.util
import sys
needed={'fastapi':'fastapi','uvicorn':'uvicorn','dotenv':'python-dotenv',
        'requests':'requests','winotify':'winotify'}
missing=[p for mod,p in needed.items() if importlib.util.find_spec(mod) is None]
print('[MISSING] '+', '.join(missing) if missing else '[OK] Python dependencies installed.')
sys.exit(1 if missing else 0)
