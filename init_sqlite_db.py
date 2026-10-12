"""Create or upgrade the SQLite database. The app also does this automatically on start-up.

    python init_sqlite_db.py            # create tables / migrate old data
    python init_sqlite_db.py --reprice  # reload car_parts_prices.json into the price list
"""
import sys

import database

if __name__ == "__main__":
    database.init_db()
    if "--reprice" in sys.argv:
        with database.db() as conn:
            database.seed_prices(conn)
        print("Price list reloaded from car_parts_prices.json")
    print(f"Database ready: {database.DB_PATH}")
