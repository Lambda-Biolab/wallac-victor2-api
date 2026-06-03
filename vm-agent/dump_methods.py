"""List every interface + method (with arg specs) in the Wallac 1420
Instrument Server TypeLib (doc 94). Read-only; introspects the generated
comtypes wrapper, does not contact the instrument.
"""

import comtypes
import comtypes.client

OUT = r"C:\install\methods.txt"
TLB = ("{08851F21-9C03-11CE-BAC1-857F25C070DD}", 2, 0)


def log(msg):
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(str(msg) + "\n")


def main():
    open(OUT, "w").close()
    mod = comtypes.client.GetModule(TLB)
    for nm in sorted(dir(mod)):
        obj = getattr(mod, nm)
        if not (isinstance(obj, type) and issubclass(obj, comtypes.IUnknown)):
            continue
        entries = list(getattr(obj, "_disp_methods_", []) or [])
        entries += list(getattr(obj, "_methods_", []) or [])
        if not entries:
            continue
        log(f"### interface {nm}")
        for e in entries:
            name = getattr(e, "name", None)
            argspec = getattr(e, "argspec", None)
            restype = getattr(e, "restype", None)
            args = []
            if argspec:
                for a in argspec:
                    try:
                        flags, atype, aname = a[0], a[1], a[2]
                        args.append(
                            "{} {} [{}]".format(
                                getattr(atype, "__name__", atype), aname, flags
                            )
                        )
                    except Exception:
                        args.append(repr(a))
            log(
                "   {} -> {} ({})".format(
                    name, getattr(restype, "__name__", restype), ", ".join(args)
                )
            )


if __name__ == "__main__":
    main()
