"""Discover the MLR.MDB schema for protocol listing (doc 95 /protocols).

The COM API has no "list protocols" method -- the GUI dropdown is populated
from the Jet database. This copies the live DB (to avoid lock conflicts with
the running OEM stack) and dumps tables + protocol-like table contents via DAO.
Read-only discovery tool. Output -> C:\\Users\\Public\\protocols.txt.
"""

import shutil
import traceback

SRC = r"C:\Program Files\Wallac\Wallac1420\Data\Mlr3.mdb"
TMP = r"C:\Users\Public\mlr3_copy.mdb"
OUT = r"C:\Users\Public\protocols.txt"
HINTS = ("prot", "assay", "meas", "label", "method")


def log(msg):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(str(msg) + "\n")


def main():
    open(OUT, "w").close()
    import comtypes
    import comtypes.client

    comtypes.CoInitialize()
    try:
        shutil.copy(SRC, TMP)
        log(f"copied DB -> {TMP}")
    except Exception:
        log("copy failed (will try live)\n" + traceback.format_exc())

    eng = comtypes.client.CreateObject("DAO.DBEngine.36")
    db = None
    for target in (TMP, SRC):
        try:
            db = eng.OpenDatabase(target, False, True)  # not excl, read-only
            log(f"opened {target}")
            break
        except Exception:
            log(f"open {target} failed\n{traceback.format_exc()}")
    if db is None:
        return 1

    tds = db.TableDefs
    names = []
    for i in range(tds.Count):
        nm = tds.Item(i).Name
        if not nm.startswith("MSys") and not nm.startswith("~"):
            names.append(nm)
    log("USER TABLES ({}): {}".format(len(names), ", ".join(names)))

    for nm in names:
        if not any(h in nm.lower() for h in HINTS):
            continue
        log(f"\n=== TABLE {nm} ===")
        try:
            rs = db.OpenRecordset(f"SELECT * FROM [{nm}]")
            fields = [rs.Fields.Item(j).Name for j in range(rs.Fields.Count)]
            log(f"fields: {fields}")
            n = 0
            while not rs.EOF and n < 15:
                row = {}
                for f in fields:
                    try:
                        row[f] = rs.Fields.Item(f).Value
                    except Exception as exc:
                        row[f] = f"<{exc}>"
                log(f"row: {row}")
                rs.MoveNext()
                n += 1
            rs.Close()
        except Exception:
            log("dump failed\n" + traceback.format_exc())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
