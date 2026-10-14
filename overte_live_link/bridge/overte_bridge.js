//
//  overte_bridge.js — Overte side of the Blender Live Link.
//
//  Connects out to the WebSocket server hosted by the Blender add-on, streams
//  entity changes to Blender and applies Blender's edits to the domain.
//
//  Runs in two places:
//    * as a domain Persistent Script (an assignment-client Agent; headless,
//      always on, full edit rights), or
//    * as a client script in Interface (Edit > Running Scripts > From URL),
//      using the signed-in user's permissions.
//
//  Blender serves this file at http://<blender-host>:<port>/overte_bridge.js
//  and fills in the WebSocket URL below. When hosting it yourself, edit WS_URL.
//
//  Distributed under the GPL-3.0-or-later license (see the add-on manifest).
//
(function () {
    "use strict";

    var WS_URL = "__OVERTE_LIVE_LINK_WS_URL__";
    var PROTOCOL_VERSION = 1;
    var RECONNECT_MS = 3000;
    var NULL_ID = "{00000000-0000-0000-0000-000000000000}";
    var IS_AGENT = typeof EntityViewer !== "undefined";

    // Properties streamed to Blender. Everything else is left untouched in Overte.
    var PROPS = [
        "type", "name", "position", "rotation", "dimensions", "registrationPoint", "parentID",
        "color", "shape", "modelURL", "visible", "locked", "lastEdited", "entityHostType",
        "intensity", "falloffRadius", "isSpotlight", "exponent", "cutoff",
        "text", "textColor", "lineHeight", "imageURL", "sourceUrl"
    ];

    var config = {
        center: { x: 0, y: 0, z: 0 },
        radius: 16384,   // Overte domains span +/-16 km
        pollMs: 100
    };

    var ws = null;
    var connected = false;
    var pollTimer = null;
    var queryTimer = null;
    var reconnectTimer = null;
    var DELETE_GRACE_MS = 4000;
    var known = {};          // entity id -> lastEdited we last reported (or caused)
    var missingSince = {};   // entity id -> time it was first found missing
    var wantFull = false;    // Blender asked for a full snapshot
    var startedAt = Date.now();
    var lastCount = -1;
    var stableSince = 0;
    var pendingUpload = null; // header of an upload waiting for its binary frame

    function log(msg) {
        print("[OverteLiveLink] " + msg);
    }

    function send(obj) {
        if (ws && connected) {
            ws.send(JSON.stringify(obj));
        }
    }

    function isDomainEntity(props) {
        // Skip avatar entities (wearables) and local-only entities (UI overlays).
        return !props.entityHostType || props.entityHostType === "domain";
    }

    function getProps(id) {
        var p = Entities.getEntityProperties(id, PROPS);
        if (!p || !p.type || p.type === "Unknown") {
            return null;
        }
        p.id = id;
        // getEntityProperties adds a few derived values we don't need.
        delete p.naturalDimensions;
        delete p.naturalPosition;
        delete p.parentJointIndex;
        return p;
    }

    function currentIds() {
        return Entities.findEntities(config.center, config.radius);
    }

    // The entity tree fills in gradually after connecting to a domain. A full
    // snapshot is only taken once the entity count has been stable for a while,
    // so Blender is never told an entity is missing just because it has not
    // arrived yet.
    function treeReady(count) {
        var now = Date.now();
        if (count !== lastCount) {
            lastCount = count;
            stableSince = now;
        }
        var minUptime = IS_AGENT ? 3000 : 1000;
        return now - startedAt > minUptime && now - stableSince > 1500;
    }

    function poll() {
        var ids = currentIds();
        if (wantFull && !treeReady(ids.length)) {
            return;
        }
        var seen = {};
        var upserts = [];
        var allIds = [];
        for (var i = 0; i < ids.length; i++) {
            var id = ids[i];
            var meta = Entities.getEntityProperties(id, ["lastEdited", "entityHostType"]);
            if (!meta || !isDomainEntity(meta)) {
                continue;
            }
            seen[id] = true;
            allIds.push(id);
            if (wantFull || known[id] !== meta.lastEdited) {
                var props = getProps(id);
                if (props) {
                    known[id] = props.lastEdited;
                    upserts.push(props);
                }
            }
        }
        var deletes = [];
        // An agent's entity tree can drop an entity for about a second after it
        // is edited and then receive it again. Report a deletion only once the
        // entity has stayed gone for DELETE_GRACE_MS.
        var now = Date.now();
        for (var k in known) {
            if (!known.hasOwnProperty(k)) {
                continue;
            }
            if (seen[k]) {
                delete missingSince[k];
                continue;
            }
            var still = Entities.getEntityProperties(k, ["type"]);
            if (still && still.type && still.type !== "Unknown") {
                delete missingSince[k];
                continue;
            }
            if (!missingSince[k]) {
                missingSince[k] = now;
            } else if (now - missingSince[k] > DELETE_GRACE_MS) {
                deletes.push(k);
                delete known[k];
                delete missingSince[k];
            }
        }
        if (wantFull) {
            send({ type: "changes", full: true, upserts: upserts, deletes: [], ids: allIds });
            wantFull = false;
        } else if (upserts.length || deletes.length) {
            send({ type: "changes", full: false, upserts: upserts, deletes: deletes });
        }
    }

    function markEdited(id) {
        // Our own edit must not echo back to Blender as a remote change.
        var meta = Entities.getEntityProperties(id, ["lastEdited"]);
        if (meta) {
            known[id] = meta.lastEdited;
        }
    }

    // ------------------------------------------------------------- commands
    function handleEdit(msg) {
        var results = [];
        (msg.edits || []).forEach(function (e) {
            var p = e.props;
            var lock = Entities.getEntityProperties(e.id, ["locked"]);
            if (lock && lock.locked && p.locked !== false) {
                // The entity server refuses edits to locked entities even though
                // the local tree would accept them; reject here so Blender reverts.
                results.push({ id: e.id, ok: false, props: getProps(e.id) });
                return;
            }
            if ((p.position || p.rotation) && p.parentID === undefined) {
                // editEntity converts world position/rotation to local using the
                // parentID *in the edit*; without it a child's world values
                // would be stored as local ones.
                var cur = Entities.getEntityProperties(e.id, ["parentID", "parentJointIndex"]);
                if (cur && cur.parentID && cur.parentID !== NULL_ID) {
                    p.parentID = cur.parentID;
                    p.parentJointIndex = cur.parentJointIndex;
                }
            }
            // The server applies nothing but the lock change in an edit that
            // unlocks an entity, so unlock first and send the rest separately;
            // when locking, apply the rest first.
            var lockChange = p.locked !== undefined && lock && p.locked !== lock.locked;
            var result;
            if (lockChange) {
                var rest = {};
                for (var key in p) {
                    if (p.hasOwnProperty(key) && key !== "locked") {
                        rest[key] = p[key];
                    }
                }
                if (p.locked) {
                    result = Entities.editEntity(e.id, rest);
                    Entities.editEntity(e.id, { locked: true });
                } else {
                    result = Entities.editEntity(e.id, { locked: false });
                    if (Object.keys(rest).length) {
                        result = Entities.editEntity(e.id, rest);
                    }
                }
            } else {
                result = Entities.editEntity(e.id, p);
            }
            var ok = !!result && result !== NULL_ID;
            markEdited(e.id);
            // On failure (locked entity, no permission) send the real state so
            // Blender can revert its copy.
            results.push({ id: e.id, ok: ok, props: ok ? undefined : getProps(e.id) });
        });
        send({ type: "edited", req: msg.req, results: results });
    }

    // Properties that describe an entity's identity or runtime state rather
    // than its content; dropped when cloning.
    var NOT_CLONED = [
        "id", "created", "age", "ageAsText", "lastEdited", "lastEditedBy", "entityHostType",
        "owningAvatarID", "clientOnly", "avatarEntity", "localEntity", "naturalDimensions",
        "naturalPosition", "boundingBox", "renderInfo", "queryAACube", "certificateID",
        "certificateType", "staticCertificateVersion", "localPosition", "localRotation",
        "localVelocity", "localAngularVelocity", "localDimensions"
    ];

    function cloneProps(sourceId, overrides) {
        var base = Entities.getEntityProperties(sourceId);
        if (!base || !base.type) {
            return overrides;
        }
        NOT_CLONED.forEach(function (k) {
            delete base[k];
        });
        for (var k in overrides) {
            if (overrides.hasOwnProperty(k)) {
                base[k] = overrides[k];
            }
        }
        return base;
    }

    function handleAdd(msg) {
        var items = [];
        (msg.items || []).forEach(function (it) {
            var props = it.cloneOf ? cloneProps(it.cloneOf, it.props) : it.props;
            var id = Entities.addEntity(props);
            var ok = id && id !== NULL_ID;
            if (ok) {
                markEdited(id);
            }
            items.push({ tmp: it.tmp, id: ok ? id : null, props: ok ? getProps(id) : null });
        });
        send({ type: "added", req: msg.req, items: items });
    }

    function handleDelete(msg) {
        (msg.ids || []).forEach(function (id) {
            Entities.deleteEntity(id);
            delete known[id];
        });
    }

    function handleConfigure(msg) {
        if (msg.center) {
            config.center = msg.center;
        }
        if (msg.radius) {
            config.radius = msg.radius;
        }
        if (msg.pollMs) {
            config.pollMs = Math.max(20, msg.pollMs);
            restartPolling();
        }
        if (IS_AGENT) {
            EntityViewer.setPosition(config.center);
            EntityViewer.setCenterRadius(config.radius);
            EntityViewer.queryOctree();
        }
    }

    function handleUpload(msg) {
        // The next binary frame carries the file content.
        pendingUpload = msg;
    }

    function finishUpload(buffer) {
        var msg = pendingUpload;
        pendingUpload = null;
        Assets.putAsset({ data: buffer, path: msg.path }, function (error, result) {
            send({
                type: "uploaded", req: msg.req, error: error || null,
                url: result ? result.url : null, hash: result ? result.hash : null
            });
        });
    }

    function handleFetch(msg) {
        var url = msg.url;
        if (url.indexOf("atp:") === 0) {
            Assets.getAsset({ url: url, responseType: "arraybuffer" }, function (error, result) {
                if (error) {
                    send({ type: "fetched", req: msg.req, error: String(error) });
                    return;
                }
                send({ type: "fetched", req: msg.req, error: null, size: result.response.byteLength });
                ws.send(result.response);
            });
        } else {
            send({ type: "fetched", req: msg.req, error: "only atp: URLs are fetched through the bridge" });
        }
    }

    function onMessage(event) {
        if (typeof event.data !== "string") {
            if (pendingUpload) {
                finishUpload(event.data);
            }
            return;
        }
        var msg;
        try {
            msg = JSON.parse(event.data);
        } catch (e) {
            log("bad message: " + e);
            return;
        }
        try {
            switch (msg.type) {
                case "configure": handleConfigure(msg); break;
                case "sync": known = {}; wantFull = true; break;
                case "edit": handleEdit(msg); break;
                case "add": handleAdd(msg); break;
                case "delete": handleDelete(msg); break;
                case "upload": handleUpload(msg); break;
                case "fetch": handleFetch(msg); break;
                case "ping": send({ type: "pong", req: msg.req }); break;
                default: log("unknown message type " + msg.type);
            }
        } catch (e) {
            log("error handling " + msg.type + ": " + e);
            send({ type: "error", req: msg.req, message: String(e), about: msg.type });
        }
    }

    // ------------------------------------------------------- connection
    // Polling reschedules itself: on large domains a poll can take a while, so
    // the next one waits at least 3x as long as the last took, keeping the
    // agent responsive (about a quarter of its time at most goes to polling).
    function schedulePoll(delay) {
        pollTimer = Script.setTimeout(function () {
            pollTimer = null;
            if (!connected) {
                return;
            }
            var t0 = Date.now();
            try {
                poll();
            } catch (e) {
                log("poll error: " + e);
            }
            schedulePoll(Math.max(config.pollMs, 3 * (Date.now() - t0)));
        }, delay);
    }

    function restartPolling() {
        if (pollTimer) {
            Script.clearTimeout(pollTimer);
            pollTimer = null;
        }
        if (connected) {
            schedulePoll(config.pollMs);
        }
    }

    function scheduleReconnect() {
        if (!reconnectTimer) {
            reconnectTimer = Script.setTimeout(function () {
                reconnectTimer = null;
                connect();
            }, RECONNECT_MS);
        }
    }

    function connect() {
        log("connecting to " + WS_URL + (IS_AGENT ? " (agent)" : " (interface)"));
        ws = new WebSocket(WS_URL);
        ws.onopen = function () {
            connected = true;
            known = {};
            log("connected");
            send({
                type: "hello",
                version: PROTOCOL_VERSION,
                mode: IS_AGENT ? "agent" : "interface",
                canRez: Entities.canRez(),
                canAdjustLocks: Entities.canAdjustLocks(),
                canWriteAssets: typeof Entities.canWriteAssets === "function" ? Entities.canWriteAssets() : null
            });
            restartPolling();
        };
        ws.onmessage = onMessage;
        ws.onclose = function () {
            if (connected) {
                log("disconnected");
            }
            connected = false;
            restartPolling();
            scheduleReconnect();
        };
        ws.onerror = function () {
            // onclose follows; it schedules the reconnect.
        };
    }

    if (IS_AGENT) {
        EntityViewer.setPosition(config.center);
        EntityViewer.setCenterRadius(config.radius);
        queryTimer = Script.setInterval(function () {
            EntityViewer.queryOctree();
        }, 1000);
    }

    Script.scriptEnding.connect(function () {
        if (pollTimer) {
            Script.clearTimeout(pollTimer);
        }
        if (queryTimer) {
            Script.clearInterval(queryTimer);
        }
        if (ws && connected) {
            ws.close();
        }
    });

    connect();
}());
