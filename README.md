# draw2sozi

Überträgt die Ebenen einer LibreOffice-Draw-Zeichnung (.odg) in ein SVG, das
[Sozi](https://github.com/sozi-projects/Sozi) als Ebenen erkennt.

LibreOffice Draw schreibt beim SVG-Export keine Ebenen. draw2sozi liest die
Ebenenzuordnung aus der .odg, ordnet sie den Formen im SVG-Export zu und legt
je Ebene eine Gruppe mit ID direkt unter die SVG-Wurzel
(`inkscape:groupmode="layer"`).

## Voraussetzungen

- Python 3.8 oder neuer
- `defusedxml`: `python -m pip install --user defusedxml`
- LibreOffice (wird unter Windows in `C:\Program Files\LibreOffice` gefunden,
  sonst mit `--soffice` angeben)

## Aufruf

```
python draw2sozi.py zeichnung.odg                  # erzeugt zeichnung.sozi-ebenen.svg
python draw2sozi.py zeichnung.odg --out folie.svg  # anderer Ausgabename
python draw2sozi.py zeichnung.odg --watch          # nach jedem Speichern in Draw neu erzeugen
python draw2sozi.py zeichnung.odg --svg export.svg # vorhandenen SVG-Export verwenden
```

Der Export läuft ohne Oberfläche mit einem eigenen, temporären
LibreOffice-Profil und funktioniert auch, während die Zeichnung in Draw offen ist.

## Regeln

- Ebenen werden in der Reihenfolge der Ebenenliste der .odg gestapelt (erste
  Ebene unten). Leere Ebenen werden weggelassen.
- Gruppen mit Formen aus mehreren Ebenen kommen auf eine eigene Ebene
  «Gruppen» (zuoberst, mit `--gruppen-unten` zuunterst).
- Geänderte Überdeckungen werden mit Form und Position gemeldet.
- **Feste IDs:** Hat eine Form oder Gruppe in Draw einen Namen
  (Rechtsklick → Name…), wird der Name ihre ID im SVG. Sozi-Rahmen, die an
  dieser Form verankert sind, bleiben dann auch nach Änderungen in Draw an der
  richtigen Form. Ohne Namen vergibt LibreOffice fortlaufende IDs (`id3`, `id4`,
  …), die sich beim Einfügen oder Umsortieren verschieben.
- Fehlt ein Titel, wird der Dateiname als `<title>` eingetragen (Sozi zeigt
  sonst «Untitled»).
- Nur einseitige Zeichnungen; bei mehreren Seiten bricht das Skript mit einer
  Meldung ab.

## Getestet

Windows 11, LibreOffice 26.2.6.3, Python 3.14, Sozi 24.11 (Entwicklungsstand):
Formen, Gruppen über mehrere Ebenen, Textrahmen, Beschriftung in Formen,
eingebettete Bilder, Verbinder, Masslinien, gedrehte Formen, mehrere Seiten
(Abbruch).
