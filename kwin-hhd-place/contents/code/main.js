// Z13 hhd-ui placement
//
// WHY A SCRIPT AND NOT A KWIN RULE
//   KWin rules can only pin an ABSOLUTE position, so any resolution or scale
//   change strands the window. The built-in placement policies have no
//   "middle-right" or "bottom-left" option either -- only Centered/ZeroCornered.
//   A script can read the current screen geometry and do the arithmetic, so the
//   placement follows the display instead of being baked in.
//
// TO CHANGE THE CORNER
//   Edit PLACEMENT below. Supported: "middle-right", "bottom-left",
//   "top-left", "middle-left", "center".

var PLACEMENT = "middle-right";
var MARGIN = 16;                       // logical px from the screen edge
var MATCH = /hhd/i;                    // matches dev-hhd-hhd-ui and dev.hhd.hhd-ui

function place(w) {
    if (!w || !w.normalWindow) return;
    if (!MATCH.test(String(w.resourceClass))) return;

    var area = workspace.clientArea(KWin.PlacementArea, w);
    if (!area) return;

    var g = w.frameGeometry;
    var x, y;

    switch (PLACEMENT) {
    case "bottom-left":
        x = area.x + MARGIN;
        y = area.y + area.height - g.height - MARGIN;
        break;
    case "top-left":
        x = area.x + MARGIN;
        y = area.y + MARGIN;
        break;
    case "middle-left":
        x = area.x + MARGIN;
        y = area.y + Math.round((area.height - g.height) / 2);
        break;
    case "center":
        x = area.x + Math.round((area.width - g.width) / 2);
        y = area.y + Math.round((area.height - g.height) / 2);
        break;
    default: // middle-right
        x = area.x + area.width - g.width - MARGIN;
        y = area.y + Math.round((area.height - g.height) / 2);
    }

    // Never let it land off-screen if the window is larger than the area.
    if (x < area.x) x = area.x;
    if (y < area.y) y = area.y;

    w.frameGeometry = { x: x, y: y, width: g.width, height: g.height };
}

// Place on appearance. Electron sizes the window slightly after mapping, so
// re-apply once the geometry settles, otherwise we position a stale size.
function hook(w) {
    place(w);
    if (w.frameGeometryChanged) {
        var done = false;
        w.frameGeometryChanged.connect(function () {
            if (done) return;
            done = true;
            place(w);
        });
    }
}

if (workspace.windowAdded) {
    workspace.windowAdded.connect(hook);      // KWin 6
} else if (workspace.clientAdded) {
    workspace.clientAdded.connect(hook);      // KWin 5 fallback
}

// Re-place existing windows when the screen layout changes (scale/resolution).
function replaceAll() {
    var ws = workspace.windowList ? workspace.windowList() : workspace.clientList();
    for (var i = 0; i < ws.length; i++) place(ws[i]);
}
if (workspace.screensChanged)         workspace.screensChanged.connect(replaceAll);
if (workspace.virtualScreenSizeChanged) workspace.virtualScreenSizeChanged.connect(replaceAll);
