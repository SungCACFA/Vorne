import sqlite3, glob, os

hits = glob.glob("cache/*.db") + glob.glob("*.db") + glob.glob("**/cache/*.db", recursive=True)
if not hits:
    raise SystemExit("No .db found. Run from the folder holding cache/")
path = hits[0]
print("cache:", os.path.abspath(path), round(os.path.getsize(path)/1e6, 1), "MB\n")

con = sqlite3.connect(path)
for (name,) in con.execute(
    "select name from sqlite_master where type='table' order by name"
):
    n = con.execute(f'select count(*) from "{name}"').fetchone()[0]
    cols = [r[1] for r in con.execute(f'pragma table_info("{name}")')]
    print(f"{name}  ({n:,} rows)")
    print("   " + ", ".join(cols) + "\n")