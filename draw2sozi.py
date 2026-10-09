#!/usr/bin/env python3
"""draw2sozi – Ebenen aus LibreOffice Draw in ein Sozi-/Inkscape-taugliches SVG übertragen.

LibreOffice Draw schreibt beim SVG-Export keine Ebenen. Dieses Skript liest
die Ebenenzuordnung aus der .odg-Datei, ordnet sie den Formen im SVG-Export zu
(gleiche Reihenfolge) und schreibt ein neues SVG, in dem jede Ebene eine Gruppe
mit ID direkt unter der SVG-Wurzel ist. Sozi erkennt solche Gruppen als Ebenen.

Aufruf:
    python draw2sozi.py zeichnung.odg                 # SVG-Export über LibreOffice
    python draw2sozi.py zeichnung.odg --svg export.svg # vorhandenen Export verwenden

Ausgabe: zeichnung.sozi-ebenen.svg (oder --out)

Regeln:
  * Ebenen werden in der Reihenfolge der Ebenenliste der .odg gestapelt
    (erste Ebene unten). Leere Ebenen werden weggelassen.
  * Gruppen mit Formen aus mehreren Ebenen kommen auf eine eigene Ebene
    «Gruppen» (Standard: zuoberst, --gruppen-unten legt sie zuunterst).
  * Die Reihenfolge innerhalb einer Ebene bleibt wie in der Zeichnung.
  * Überlappende Formen, deren Stapelreihenfolge sich dadurch ändert, werden
    gemeldet.
  * Hat eine Form oder Gruppe in Draw einen Namen (Rechtsklick → Name…), wird
    dieser Name ihre ID im SVG. Sozi-Rahmen, die an der Form verankert sind,
    bleiben so auch nach Änderungen in Draw an der richtigen Form. Ungültige,
    doppelte oder schon vergebene Namen werden gemeldet und nicht übernommen.

Voraussetzungen: Python 3.8+, Paket defusedxml (pip install defusedxml),
für den automatischen Export LibreOffice (soffice).
Nur einseitige Zeichnungen werden unterstützt.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET  # nur zum Erzeugen/Schreiben, nicht zum Parsen fremder Daten

try:
    from defusedxml.ElementTree import fromstring as safe_fromstring  # SSV/XML: kein XXE, keine Entity-Expansion
except ImportError:  # pragma: no cover
    sys.exit("Fehler: Paket 'defusedxml' fehlt. Installation: pip install defusedxml")

NS = {
    "svg": "http://www.w3.org/2000/svg",
    "xlink": "http://www.w3.org/1999/xlink",
    "ooo": "http://xml.openoffice.org/svg/export",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "osvg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "inkscape": "http://www.inkscape.org/namespaces/inkscape",
}
D = "{%s}" % NS["draw"]
OS = "{%s}" % NS["osvg"]
S = "{%s}" % NS["svg"]

MAX_XML_BYTES = 64 * 1024 * 1024   # Schutz gegen übergrosse/gepackte Bomben (MM-2)
GRUPPEN_EBENE = "Gruppen"
ART = {"ellipse": "Ellipse", "rectangle": "Rechteck", "isosceles-triangle": "Dreieck",
       "smiley": "Smiley", "g": "Gruppe", "frame": "Textrahmen", "line": "Linie",
       "rect": "Rechteck", "path": "Pfad", "polygon": "Polygon", "polyline": "Linienzug",
       "custom-shape": "Form", "connector": "Verbinder", "measure": "Masslinie"}
UNITS_TO_HMM = {"cm": 1000.0, "mm": 100.0, "in": 2540.0, "pt": 2540.0 / 72, "pc": 2540.0 / 6}


class Abbruch(Exception):
    """Fachlicher Fehler: Verarbeitung wird mit Meldung abgebrochen (fail secure)."""


# --------------------------------------------------------------------------- ODG

def read_zip_member(odg_path, name):
    with zipfile.ZipFile(odg_path) as z:
        try:
            info = z.getinfo(name)
        except KeyError:
            raise Abbruch(f"'{name}' fehlt in {odg_path} – ist das eine .odg-Datei?")
        if info.file_size > MAX_XML_BYTES:
            raise Abbruch(f"'{name}' ist grösser als {MAX_XML_BYTES // 1024 // 1024} MB – abgebrochen.")
        return z.read(name)


def length_hmm(value):
    """ODF-Längenangabe ('1.25cm') in 1/100 mm; None, wenn nicht auswertbar."""
    if not value:
        return None
    m = re.fullmatch(r"\s*(-?[0-9.]+)\s*([a-z]+)\s*", value)
    if not m or m.group(2) not in UNITS_TO_HMM:
        return None
    return float(m.group(1)) * UNITS_TO_HMM[m.group(2)]


def layers_of(el):
    """Menge der Ebenen einer Form bzw. aller Formen einer Gruppe."""
    if el.tag == D + "g":
        result = set()
        for c in el:
            if c.tag.startswith(D):
                result |= layers_of(c)
        return result
    return {el.get(D + "layer") or "layout"}


def read_odg(odg_path):
    styles = safe_fromstring(read_zip_member(odg_path, "styles.xml"))
    content = safe_fromstring(read_zip_member(odg_path, "content.xml"))

    layer_order = []
    for root in (styles, content):
        for ls in root.iter(D + "layer-set"):
            for layer in ls:
                name = layer.get(D + "name")
                if name and name not in layer_order:
                    layer_order.append(name)
    if not layer_order:
        layer_order = ["layout"]

    pages = list(content.iter(D + "page"))
    if len(pages) != 1:
        raise Abbruch(f"Die Zeichnung hat {len(pages)} Seiten. Unterstützt wird nur eine Seite.")

    items = []  # Formen/Gruppen der obersten Ebene, in Dokumentreihenfolge
    for el in pages[0]:
        if not el.tag.startswith(D):
            continue  # z. B. office:forms, Präsentationsnotizen
        tag = el.tag[len(D):]
        if tag in ("layer-set",):
            continue
        layers = layers_of(el)
        geo = el.find(D + "enhanced-geometry")
        typ = geo.get(D + "type") if geo is not None else tag
        items.append({
            "art": ART.get(typ, typ),
            "tag": tag,
            "el": el,
            "layers": layers,
            "x": length_hmm(el.get(OS + "x")),
            "y": length_hmm(el.get(OS + "y")),
        })
    return layer_order, items


# --------------------------------------------------------------------------- SVG

def find_soffice(explicit):
    if explicit:
        return explicit
    for cand in ("soffice", "libreoffice"):
        p = shutil.which(cand)
        if p:
            return p
    for p in (r"C:\Program Files\LibreOffice\program\soffice.exe",
              r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
              "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if os.path.isfile(p):
            return p
    raise Abbruch("LibreOffice (soffice) nicht gefunden. Pfad mit --soffice angeben "
                  "oder einen vorhandenen SVG-Export mit --svg übergeben.")


def export_svg(odg_path, soffice):
    """SVG-Export über LibreOffice ohne Oberfläche, mit eigenem temporärem Profil."""
    tmp = tempfile.mkdtemp(prefix="draw2sozi-")
    profile = tempfile.mkdtemp(prefix="draw2sozi-profil-")
    try:
        profile_url = "file:///" + profile.replace("\\", "/").lstrip("/")
        # LF-6/GCP-3: fester Befehl, Argumentliste, keine Shell
        cmd = [soffice, f"-env:UserInstallation={profile_url}", "--headless",
               "--convert-to", "svg:draw_svg_Export", "--outdir", tmp, os.path.abspath(odg_path)]
        try:
            subprocess.run(cmd, check=True, timeout=300,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except subprocess.TimeoutExpired:
            raise Abbruch("LibreOffice-Export hat nach 300 s nicht geantwortet.")
        except (subprocess.CalledProcessError, OSError) as e:
            raise Abbruch(f"LibreOffice-Export fehlgeschlagen: {e}")
        out = os.path.join(tmp, os.path.splitext(os.path.basename(odg_path))[0] + ".svg")
        if not os.path.isfile(out):
            raise Abbruch("LibreOffice hat kein SVG erzeugt.")
        with open(out, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)      # PDR-4: temporäre Kopien löschen
        shutil.rmtree(profile, ignore_errors=True)


def bbox_of(g):
    """Erste BoundingBox (x, y, w, h) innerhalb eines Formelements."""
    for r in g.iter(S + "rect"):
        if r.get("class") == "BoundingBox":
            try:
                return tuple(float(r.get(k)) for k in ("x", "y", "width", "height"))
            except (TypeError, ValueError):
                return None
    return None


def union(boxes):
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes); y1 = max(b[1] + b[3] for b in boxes)
    return (x0, y0, x1 - x0, y1 - y0)


def group_bbox(g):
    return union([bbox_of(c) or group_bbox(c) for c in g])


def read_svg(svg_bytes):
    if len(svg_bytes) > MAX_XML_BYTES:
        raise Abbruch("SVG-Datei ist zu gross.")
    if svg_bytes[:2] == b"\x1f\x8b":
        raise Abbruch("Komprimiertes SVG (.svgz) – bitte unkomprimiertes .svg übergeben.")
    root = safe_fromstring(svg_bytes)
    if root.tag != S + "svg":
        raise Abbruch("Keine SVG-Datei.")
    pages = [g for g in root.iter(S + "g") if g.get("class") == "Page"]
    if len(pages) != 1:
        raise Abbruch(f"Im SVG {len(pages)} Seiten gefunden – erwartet wird genau eine "
                      "(LibreOffice-Draw-Export).")
    slide = next((g for g in root.iter(S + "g") if g.get("class") == "Slide"), None)
    master = next((g for g in root.iter(S + "g") if g.get("class") == "Master_Slide"), None)
    shapes = [c for c in pages[0] if c.tag == S + "g"]
    return root, slide, master, shapes


# --------------------------------------------------------------------------- Ausgabe

def xml_id(name, used):
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    if not re.match(r"[A-Za-z_]", base):
        base = "ebene_" + base
    cand, i = base, 2
    while cand in used:
        cand = f"{base}_{i}"; i += 1
    used.add(cand)
    return cand


NCNAME = re.compile(r"[^\W\d][\w.-]*")


def shape_target(g):
    """Element, das im SVG die ID einer Form trägt (bei Gruppen die Gruppe selbst)."""
    if g.get("class") == "Group":
        return g
    inner = next((c for c in g if c.tag == S + "g" and c.get("id")), None)
    return inner if inner is not None else g


def collect_names(odg_el, svg_g, out, meldungen):
    """Paare (Name aus Draw, SVG-Element) sammeln, rekursiv in Gruppen."""
    name = odg_el.get(D + "name")
    if name:
        out.append((name, shape_target(svg_g)))
    if odg_el.tag == D + "g":
        odg_kids = [c for c in odg_el if c.tag.startswith(D)]
        svg_kids = [c for c in svg_g if c.tag == S + "g"]
        if len(odg_kids) != len(svg_kids):
            meldungen.append(f"Hinweis: Gruppe «{name or 'ohne Namen'}» hat in .odg und SVG unterschiedlich "
                             "viele Formen – Namen darin werden nicht übernommen.")
            return
        for o, s in zip(odg_kids, svg_kids):
            collect_names(o, s, out, meldungen)


def apply_names(root, pairs, reserved, meldungen):
    """Namen aus Draw als feste IDs setzen und Verweise im SVG nachführen.

    Ungültige, doppelte oder mit anderen IDs kollidierende Namen werden nicht
    übernommen und gemeldet; die Form behält dann die ID von LibreOffice.
    """
    count = {}
    for name, _ in pairs:
        count[name] = count.get(name, 0) + 1
    renamed = {}
    gesetzt = 0
    gemeldet = set()
    for name, el in pairs:
        if not NCNAME.fullmatch(name):
            meldungen.append(f"Name «{name}» ist als ID ungültig (erlaubt: Buchstaben, Ziffern, _ . -, "
                             "nicht mit Ziffer beginnend) – nicht übernommen.")
            continue
        if count[name] > 1:
            if name not in gemeldet:
                meldungen.append(f"Name «{name}» kommt {count[name]}-mal vor – nicht übernommen.")
                gemeldet.add(name)
            continue
        old = el.get("id")
        if name == old:
            continue
        if name in reserved:
            meldungen.append(f"Name «{name}» ist schon als ID vergeben (z. B. Ebene) – nicht übernommen.")
            continue
        el.set("id", name)
        gesetzt += 1
        if old:
            renamed[old] = name
    if renamed:
        # Verweise nachführen: ooo:id-list, xlink:href="#…", url(#…)
        token = re.compile(r"(?<![\w.-])(" + "|".join(re.escape(k) for k in renamed) + r")(?![\w.-])")
        for e in root.iter():
            for k, v in list(e.attrib.items()):
                if k == "id":
                    continue
                nv = token.sub(lambda m: renamed[m.group(1)], v)
                if nv != v:
                    e.set(k, nv)
    return gesetzt


def overlaps(a, b):
    return (a and b and a[0] < b[0] + b[2] and b[0] < a[0] + a[2]
            and a[1] < b[1] + b[3] and b[1] < a[1] + a[3])


def build(odg_path, svg_bytes, gruppen_oben=True, toleranz_mm=0.5):
    layer_order, items = read_odg(odg_path)
    root, slide, master, shapes = read_svg(svg_bytes)
    meldungen = []

    if len(items) != len(shapes):
        raise Abbruch(f"Anzahl passt nicht: {len(items)} Objekte in der .odg, {len(shapes)} im SVG. "
                      "SVG und .odg stammen vermutlich nicht vom selben Stand.")

    # Zuordnung über Reihenfolge, Plausibilitätsprüfung über Position
    zuordnung = []
    for i, (it, g) in enumerate(zip(items, shapes), start=1):
        box = bbox_of(g) if g.get("class") != "Group" else group_bbox(g)
        if it["x"] is not None and box is not None:
            if abs(it["x"] - box[0]) > toleranz_mm * 100 or abs(it["y"] - box[1]) > toleranz_mm * 100:
                meldungen.append(f"Hinweis: Objekt {i} liegt im SVG an anderer Position als in der .odg "
                                 f"({it['x']/100:.2f}/{it['y']/100:.2f} mm vs. {box[0]/100:.2f}/{box[1]/100:.2f} mm). "
                                 "Bei gedrehten Formen oder dicken Linien kann das normal sein.")
        if len(it["layers"]) == 1:
            ziel = next(iter(it["layers"]))
        else:
            ziel = GRUPPEN_EBENE
            wo_g = f"{box[0]/1000:.2f}/{box[1]/1000:.2f} cm".replace(".", ",") if box else "unbekannter Position"
            meldungen.append(f"Gruppe bei {wo_g} enthält Formen der Ebenen "
                             f"{', '.join(sorted(it['layers']))} → Ebene «{GRUPPEN_EBENE}».")
        wo = f"{box[0]/1000:.2f}/{box[1]/1000:.2f} cm".replace(".", ",") if box else "Position unbekannt"
        zuordnung.append((f"{it['art']} bei {wo}", ziel, g, box))

    reihenfolge = [l for l in layer_order]
    for _, ziel, _, _ in zuordnung:
        if ziel not in reihenfolge and ziel != GRUPPEN_EBENE:
            reihenfolge.append(ziel)
    if any(z == GRUPPEN_EBENE for _, z, _, _ in zuordnung):
        reihenfolge = [GRUPPEN_EBENE] + reihenfolge if not gruppen_oben else reihenfolge + [GRUPPEN_EBENE]
    belegt = [l for l in reihenfolge if any(z == l for _, z, _, _ in zuordnung)]

    # Stapelreihenfolge vorher/nachher vergleichen
    neu_pos = {}
    for l in belegt:
        for k, (_, z, _, _) in enumerate(zuordnung):
            if z == l:
                neu_pos[k] = len(neu_pos)
    for a in range(len(zuordnung)):
        for b in range(a + 1, len(zuordnung)):
            ia, za, _, ba = zuordnung[a]; ib, zb, _, bb = zuordnung[b]
            if neu_pos[a] > neu_pos[b] and overlaps(ba, bb):
                meldungen.append(f"Stapelung geändert: {ia} (Ebene {za}) lag unter {ib} "
                                 f"(Ebene {zb}) und liegt jetzt darüber.")

    # IDs der Ebenen festlegen, danach Namen aus Draw als feste IDs der Formen
    used_ids = {e.get("id") for e in root.iter() if e.get("id")}
    hat_master = master is not None and any(len(c) for c in master if c.get("class") == "BackgroundObjects")
    master_id = xml_id("Masterseite", used_ids) if hat_master else None
    layer_ids = {l: xml_id(l, used_ids) for l in belegt}
    namen = []
    for it, (_, _, g, _) in zip(items, zuordnung):
        collect_names(it["el"], g, namen, meldungen)
    n_ids = apply_names(root, namen, used_ids, meldungen)
    if n_ids:
        meldungen.insert(0, f"Feste IDs aus Namen in Draw: {n_ids}")

    # Neues SVG aufbauen
    for p, uri in (("", NS["svg"]), ("xlink", NS["xlink"]), ("ooo", NS["ooo"]),
                   ("inkscape", NS["inkscape"])):
        ET.register_namespace(p, uri)
    new = ET.Element(S + "svg", {k: v for k, v in root.attrib.items()})
    for child in root:
        if child.tag == S + "defs" or child.tag == S + "title":
            new.append(child)
    clip = slide.get("clip-path") if slide is not None else None

    statistik = []
    if hat_master:
        mg = ET.SubElement(new, S + "g", {"id": master_id,
                                          "{%s}groupmode" % NS["inkscape"]: "layer",
                                          "{%s}label" % NS["inkscape"]: "Masterseite"})
        mg.append(master)
        statistik.append(("Masterseite", 1))
    for l in belegt:
        attrs = {"id": layer_ids[l],
                 "{%s}groupmode" % NS["inkscape"]: "layer",
                 "{%s}label" % NS["inkscape"]: l}
        if clip:
            attrs["clip-path"] = clip
        lg = ET.SubElement(new, S + "g", attrs)
        n = 0
        for _, z, g, _ in zuordnung:
            if z == l:
                lg.append(g); n += 1
        statistik.append((l, n))
    intern = {"background", "backgroundobjects", "controls", "measurelines"}
    leer = [l for l in layer_order if l not in belegt and l not in intern]
    return new, statistik, leer, meldungen


def main(argv=None):
    ap = argparse.ArgumentParser(description="Ebenen aus LibreOffice Draw für Sozi/Inkscape ins SVG übertragen.")
    ap.add_argument("odg", help="Draw-Datei (.odg)")
    ap.add_argument("--svg", help="vorhandener SVG-Export derselben Zeichnung (sonst Export über LibreOffice)")
    ap.add_argument("--out", help="Ausgabedatei (Standard: <name>.sozi-ebenen.svg)")
    ap.add_argument("--soffice", help="Pfad zu soffice bzw. soffice.exe")
    ap.add_argument("--gruppen-unten", action="store_true",
                    help=f"Ebene «{GRUPPEN_EBENE}» zuunterst statt zuoberst")
    a = ap.parse_args(argv)
    # Umgeleitete Ausgabe (Datei, Pipe) nutzt unter Windows oft cp1252: nicht abstürzen
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    try:
        if not os.path.isfile(a.odg):
            raise Abbruch(f"Datei nicht gefunden: {a.odg}")
        if a.svg:
            with open(a.svg, "rb") as f:
                svg_bytes = f.read()
        else:
            svg_bytes = export_svg(a.odg, find_soffice(a.soffice))
        new, statistik, leer, meldungen = build(a.odg, svg_bytes, gruppen_oben=not a.gruppen_unten)
        out = a.out or os.path.splitext(a.odg)[0] + ".sozi-ebenen.svg"
        if os.path.abspath(out) in (os.path.abspath(a.odg), os.path.abspath(a.svg or "")):
            raise Abbruch("Ausgabedatei würde eine Eingabedatei überschreiben.")
        ET.ElementTree(new).write(out, encoding="UTF-8", xml_declaration=True)
    except (Abbruch, zipfile.BadZipFile, ET.ParseError) as e:
        print(f"Abbruch: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # EE-7: keine Stacktraces an den Nutzer
        print(f"Unerwarteter Fehler ({type(e).__name__}): {e}", file=sys.stderr)
        return 3

    print(f"Geschrieben: {out}")
    print("Ebenen (unten → oben):")
    for name, n in statistik:
        print(f"  {name}: {n} Objekt(e)")
    if leer:
        print("Leere Ebenen (weggelassen): " + ", ".join(leer))
    for m in meldungen:
        print("  " + m)
    return 0


if __name__ == "__main__":
    sys.exit(main())
