"""Dump the Wallac 1420 Instrument Server TypeLib (doc 94) to a readable file.

Generates the comtypes wrapper from the registered TypeLib
({08851F21-9C03-11CE-BAC1-857F25C070DD} v2.0) and records the generated
module path so the full interface/method signatures (incl. GetCounts) can be
inspected. Read-only; does not contact the instrument.
"""

import traceback

OUT = r"C:\install\tlb_dump.txt"
TLB = ("{08851F21-9C03-11CE-BAC1-857F25C070DD}", 2, 0)


def log(msg):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(str(msg) + "\n")
    try:
        print(msg)
    except Exception:
        pass


def main():
    open(OUT, "w").close()
    log("=== TypeLib dump ===")
    try:
        import comtypes.client

        mod = comtypes.client.GetModule(TLB)
        log("friendly module: {}".format(getattr(mod, "__file__", "?")))
        # the raw wrapper (with all COMMETHOD/DISPMETHOD defs) lives next to it
        import comtypes.gen as gen

        log(f"gen dir: {list(gen.__path__)[0]}")
    except Exception:
        log(traceback.format_exc())


if __name__ == "__main__":
    main()
