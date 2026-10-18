from .config import Config
from .db import Database

if __name__ == '__main__':
    Database(Config.load().database_url).initialize()
    print('Database initialized')
