// Test helper: a second, independent Agent in the domain. Its entity tree only
// holds what the entity server sends, so tests use it to check what the domain
// really contains and to act as "another user" editing entities.
(function () {
    "use strict";
    var WS_URL = "__OVERTE_LIVE_LINK_WS_URL__";
    var PROPS = ["type", "name", "position", "rotation", "dimensions", "parentID", "color", "shape",
                 "modelURL", "visible", "locked", "intensity", "falloffRadius", "isSpotlight", "cutoff",
                 "exponent", "text", "entityHostType", "lastEdited"];
    EntityViewer.setPosition({ x: 0, y: 0, z: 0 });
    EntityViewer.setCenterRadius(16384);
    Script.setInterval(function () { EntityViewer.queryOctree(); }, 250);

    var ws = null;
    function reply(req, data) {
        ws.send(JSON.stringify({ req: req, data: data }));
    }
    function list() {
        return Entities.findEntities({ x: 0, y: 0, z: 0 }, 16384).map(function (id) {
            var p = Entities.getEntityProperties(id, PROPS);
            p.id = id;
            return p;
        }).filter(function (p) { return p.entityHostType === "domain"; });
    }
    function connect() {
        ws = new WebSocket(WS_URL);
        ws.onopen = function () { ws.send(JSON.stringify({ hello: "probe" })); };
        ws.onclose = function () { Script.setTimeout(connect, 1000); };
        ws.onmessage = function (m) {
            var msg = JSON.parse(m.data);
            try {
                if (msg.op === "list") { reply(msg.req, list()); }
                if (msg.op === "get") {
                    var p = Entities.getEntityProperties(msg.id, PROPS);
                    if (p && p.type && p.type !== "Unknown") { p.id = msg.id; } else { p = null; }
                    reply(msg.req, p);
                }
                if (msg.op === "add") { reply(msg.req, Entities.addEntity(msg.props)); }
                if (msg.op === "edit") { reply(msg.req, Entities.editEntity(msg.id, msg.props)); }
                if (msg.op === "delete") { Entities.deleteEntity(msg.id); reply(msg.req, true); }
            } catch (e) {
                reply(msg.req, { error: String(e) });
            }
        };
    }
    connect();
}());
