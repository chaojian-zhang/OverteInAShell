# Overview

## How it fits together

- **The Blender add-on** runs a small server inside Blender on port 9420.
- **A bridge script** (`overte_bridge.js`) runs inside Overte and connects to Blender. It reads and writes entities through Overte's normal entity API.
- Blender hands out the bridge script itself, at a URL like `http://127.0.0.1:9420/overte_bridge.js?token=…`. You give that URL to Overte in one of two ways:
  - **Interface:** run it as a script in your client. It uses your own permissions and only sees entities your client has loaded.
  - **Persistent Script (recommended):** the domain runs it in the background with full rights, so it sees every entity and works without Interface open.

## One-time setup

1. **Back up the domain first.** Deleting an object in Blender deletes it in the world. In `http://localhost:40100`, go to *Content → Content Archives* and generate a new archive.
2. **Install the add-on.** In Blender: *Edit → Preferences → Get Extensions → ⌄ (top right) → Install from Disk…* and pick the zip.
3. **Make a dedicated .blend file**, for example `MyLocalDomain.blend`. The access token in the URL is stored in this file, which is why the file matters.
4. **Start the link.** In the 3D Viewport press **N**, open the **Overte** tab and click **Start Live Link**. Then save with **Ctrl+S** so the token is kept. For the first session you may want to untick *Sync deletions*.
5. **Quick test through Interface.** Click the copy icon next to the URL in the panel. In Interface, go to *Edit → Running Scripts → From URL* and paste it.
   - The panel should change to **"Bridge connected (Interface)"** and show a count of linked entities.
   - The domain's entities appear in Blender's **Overte** collection.
   - Move one and it should move in-world.
6. **Make it permanent.** In `http://localhost:40100`, go to *Content → Scripts → Persistent Scripts*, add the same URL and Save. The domain restarts.
   - The panel should then say **"Bridge connected (domain agent)"**.
   - Remove the Interface copy from *Running Scripts*. If you leave it, it just waits on standby, but it reloads every time you open Interface.

## Day to day

Open `MyLocalDomain.blend` and click **Start Live Link**. The bridge retries every 3 seconds, so it reconnects by itself and Interface doesn't need to be open.

Things to keep in mind:

- **Always start from that same .blend.** A new file generates a new token, and the URL saved in the domain stops working. To fix it, either reopen the right file or paste the new URL into Persistent Scripts.
- **Your own Blender objects aren't sent to Overte by themselves.** Only these go across:
  - objects in the Overte collection;
  - anything made with *Add → Overte Entity*;
  - anything you explicitly **Publish**.
- **The domain fetches the script from Blender**, so the link has to be running when the domain loads the script. If the panel stays on "Waiting for the Overte bridge…" after the domain restarts, restart the domain from the Server Console tray icon. I haven't confirmed that the domain retries on its own.

## If it doesn't connect

- **Check Blender's server:** open `http://127.0.0.1:9420/` in a browser. It should say "Overte Live Link is running."
- **Find the bridge's messages:** they start with `[OverteLiveLink]`.
  - In Interface mode they're in Interface's log.
  - For the Persistent Script they're in the `ac-*-stdout.txt` files in `C:\Users\Charles Zhang\AppData\Local\Overte\Server Console\logs`.
- **"No rez permission: edits will be rejected"** means the Interface bridge is running without you logged in as `charles`. The Persistent Script doesn't have this problem.
