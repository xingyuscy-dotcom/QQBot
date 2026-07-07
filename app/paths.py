from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_LOCAL_PATH = PROJECT_ROOT / "config.local.json"
CONFIG_EXAMPLE_PATH = PROJECT_ROOT / "config.example.json"
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
BACKUPS_DIR = PROJECT_ROOT / "backups"
DB_PATH = DATA_DIR / "bot.sqlite3"
KNOWLEDGE_DB_PATH = DATA_DIR / "knowledge.sqlite3"
HOT_DB_PATH = DATA_DIR / "hot.sqlite3"
COMMANDS_PATH = DATA_DIR / "commands.json"
MEMORIES_DIR = DATA_DIR / "memories"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
STATIC_DIR = PROJECT_ROOT / "static"
