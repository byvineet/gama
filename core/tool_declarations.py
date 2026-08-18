"""
core/tool_declarations.py — Static tool data (extracted from main.py, C3 refactor)
====================================================================================
Pure data: the spoken acknowledgment lines used while a tool runs, and the
Gemini Live `TOOL_DECLARATIONS` schema list. No logic, no imports beyond
what the literal data needs — safe to import from anywhere.
"""

from __future__ import annotations

PROCESSING_ACK_LINES = [
    "On it, sir.",
    "One moment, sir.",
    "Okay sir, working on it.",
    "Sure sir, just a moment.",
    "Getting that done, sir.",
    "Right away, sir.",
    "On it.",
    "Sure, one moment."]

# Tool- (and action-) specific natural acknowledgments spoken when a call
# crosses the 500 ms threshold. Keys are "{tool_name}_{action}" first,
# then plain "{tool_name}" as a fallback — both are checked before falling
# back to PROCESSING_ACK_LINES so common long-running operations sound
# contextual rather than generic.
_TOOL_ACK_MAP: dict[str, list[str]] = {
    "telegram_sender_send_voice": [
        "Recording your Telegram voice note now, sir.",
        "Synthesizing the voice note — one moment.",
        "Sending a Live voice note to Telegram now."],
    "telegram_sender_voice": [
        "Recording your Telegram voice note now, sir.",
        "Synthesizing the voice note — one moment."],
    "knowledge_action_index_now": [
        "Okay sir, starting the indexing now.",
        "Sure sir, I'll index that now.",
        "Starting the index, sir."],
    "knowledge_action_reindex": [
        "Okay sir, re-indexing that now.",
        "On it sir, refreshing the index.",
        "Re-indexing now, sir."],
    "knowledge_action_search": [
        "Searching through your files, sir.",
        "Let me look through the knowledge base, sir.",
        "Looking that up in the index, sir."],
    "automation_engine": [
        "Okay sir, I'll handle that.",
        "On it, sir — working through it now.",
        "Handling that automation now, sir."],
    "computer_agent": [
        "On it, sir.",
        "Handling that now, sir."],
    "terminal_command_run": [
        "Running that command, sir.",
        "On it, sir."],
    "advanced_automation": [
        "On it, sir.",
        "Handling that now, sir."],
    "file_controller": [
        "On it, sir.",
        "Taking care of that, sir."],
    "generate_image": [
        "Generating your image now, sir.",
        "I'll create that image for you, sir.",
        "Drawing that up now, sir."]
}

# Module-level reference to the assistant instance (for set_voice tool)
_assistant_instance: "GamaAssistant | None" = None

# Tool declarations for Gemini
TOOL_DECLARATIONS = [
    {"name": "open_app", "behavior": "NON_BLOCKING", "description": "Opens executable software applications by name (e.g. 'Chrome', 'Notepad', 'Spotify', 'VS Code', 'Calculator'). Do NOT call open_app for files, documents, PDFs, spreadsheets, notes, or images — use knowledge_action action='open' with path set to the file name/query instead. Set new_window=true when user asks to open a new window or instance.",
     "parameters": {"type": "OBJECT", "properties": {"app_name": {"type": "STRING", "description": "App name e.g. 'Chrome', 'Notepad'"}, "new_window": {"type": "BOOLEAN", "description": "true to open a new window/instance; false (default) to bring existing window to front."}}, "required": ["app_name"]}},
    {"name": "edge_search", "behavior": "NON_BLOCKING", "description": "Search using the user's REAL, already-installed Microsoft Edge desktop app — not a Playwright/automated browser instance. Types the query straight into Edge's own address/search bar exactly like the user typing it and pressing Enter, so it runs through whichever engine is set as Edge's own default search provider (never a hardcoded google.com/bing.com/yahoo.com URL). Use this whenever the user wants something 'searched in Edge' / 'searched in the browser' / 'searched in the search bar', or for general 'search for X' requests where they want a real, visible Edge window with results. By default this opens a NEW TAB in the same Edge window and searches there, leaving whatever the user already had open untouched — that's the right default for a bare 'search this' / 'search for X'. Only pass new_tab=false when the user explicitly asks to search 'in this tab' / 'in the current tab' / 'same tab'.",
     "parameters": {"type": "OBJECT", "properties": {"query": {"type": "STRING"}, "new_tab": {"type": "BOOLEAN", "description": "true (default) = open a fresh tab and search there; false = reuse the current tab. Only set false if the user explicitly says 'current tab'/'this tab'/'same tab'."}}, "required": ["query"]}},
    {"name": "computer_settings", "description": "Volume, brightness, power, screenshots, window management, Wi-Fi, Bluetooth. All changes are verified (read back) where possible. Destructive actions (shutdown/restart/sleep/lock) require a confirmation_code AND verbal_confirmed — ask the user for their code if not provided, and ask 'are you sure?' before setting verbal_confirmed=true. First call it without verbal_confirmed (or with it false) to trigger the confirmation prompt; only call again with verbal_confirmed=true after the user clearly says yes out loud — never set it true preemptively. NOTE: action='restart' here reboots the whole PC/OS — for the user asking Gama to restart/reboot ITSELF (the assistant app), use restart_self instead, never this.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING", "description": "volume_up|volume_down|volume_set|mute|brightness|screenshot|lock|sleep|restart|shutdown|wifi_on|wifi_off|bluetooth_on|bluetooth_off|bluetooth_status|minimize|close|switch_window"}, "value": {"type": "STRING", "description": "e.g. brightness/volume_set percentage 0-100"}, "confirmation_code": {"type": "STRING", "description": "Required for shutdown/restart/sleep/lock. Ask the user to say their code."}, "verbal_confirmed": {"type": "BOOLEAN", "description": "Required for shutdown/restart/sleep/lock. Set true only after the user has clearly said yes out loud to a spoken 'are you sure?' confirmation — never set true on the first call."}}, "required": ["action"]}},
    {"name": "live_vision", "behavior": "NON_BLOCKING", "description": "Gemini Live vision control. Continuous stream is max 1 FPS (API limit) and auto-disables after ~90s idle to save tokens. For questions like 'what am I holding?' or 'what's on my screen RIGHT NOW', call action=snapshot (optionally mode=camera|desktop) to send an exact-moment JPEG into the Live session, then describe what you see. For ongoing awareness: action=enable with mode=camera|desktop|both (camera also shows a moveable live preview on the HUD and hides the voice orb). Actions: enable, disable, status, snapshot, enable_camera, enable_desktop, enable_both.",
     "parameters": {"type": "OBJECT", "properties": {
         "action": {"type": "STRING", "description": "enable|disable|status|snapshot|enable_camera|enable_desktop|enable_both"},
         "mode": {"type": "STRING", "description": "camera (default), desktop, or both — used with enable/snapshot"},
         "camera_index": {"type": "NUMBER", "description": "Webcam device index, default 0"}
     }, "required": ["action"]}},
    {"name": "edith_screen_vision", "behavior": "NON_BLOCKING", "description": "E.D.I.T.H. Tactical Vision Engine — fast local OCR + multimodal screen/active-window visual analysis. READ-ONLY: it only looks at the screen and returns a description/answer as text — it cannot type, click, or otherwise control anything. NEVER use this to 'write'/'type'/'fill in' something into an app (Notepad, a form, a document, etc.) — for that, first generate the content yourself, then call keyboard_actions (action='type') to actually type it into the active window. Only use edith_screen_vision when the user wants Gama to look at / describe / read what's currently on screen.",

     "parameters": {"type": "OBJECT", "properties": {"prompt": {"type": "STRING", "description": "What to analyze or look for"}, "target_window_only": {"type": "BOOLEAN", "description": "If true, crop to the active foreground window"}}, "required": ["prompt"]}},
    {"name": "screen_agent", "behavior": "NON_BLOCKING",
     "description": (
         "Visual screen agent — takes a screenshot, uses Gemini vision to FIND UI elements "
         "on the 1280×800 screen by their appearance, CLICKS them, then READS the result. "
         "Use this whenever the user wants to interact with something visible on screen: "
         "'check notifications on PW', 'click the notification bell', "
         "'open my profile on GitHub', 'find the search bar and click it'. "
         "ALWAYS prefer this over mouse_actions when you need to locate something visually. "
         "For multi-step tasks (open site → find element → click → read), use action=visual_task. "
     ),
     "parameters": {"type": "OBJECT", "properties": {
         "action": {
             "type": "STRING",
             "description": (
                 "visual_task: full pipeline — open URL/app (optional) → find element → click → read result. "
                 "find_and_click: screenshot → find element by description → click. "
                 "read_screen: screenshot → read/summarise content. "
                 "screenshot_and_describe: describe the full screen."
             )
         },
         "task": {
             "type": "STRING",
             "description": "Natural-language task for visual_task. E.g. 'check notifications on PW'."
         },
         "url": {
             "type": "STRING",
             "description": "URL to open in browser before the task (optional). E.g. 'https://pw.live'."
         },
         "app": {
             "type": "STRING",
             "description": "App name to launch before the task (optional). E.g. 'Spotify'."
         },
         "element": {
             "type": "STRING",
             "description": "Description of the UI element to find and click (for find_and_click). E.g. 'notification bell icon'."
         },
         "prompt": {
             "type": "STRING",
             "description": "What to ask about the screen (for read_screen / screenshot_and_describe)."
         },
         "steps": {
             "type": "INTEGER",
             "description": "Max interaction steps for visual_task (default 3, max 6)."
         }
     }, "required": ["action"]}},
    {"name": "file_controller", "behavior": "NON_BLOCKING", "description": "File and folder management: create_folder, open_folder, delete, move (a file/folder to a new path), copy, rename, list, find. 'move' here means relocating a file or folder on disk — NOT moving the mouse cursor (use mouse_actions) and NOT rearranging a window on screen (use advanced_automation window_arrange). delete/empty_recycle_bin/format are destructive and require verbal_confirmed=true — ask 'are you sure?' first, then only set it true once the user clearly says yes out loud.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING"}, "path": {"type": "STRING"}, "src": {"type": "STRING"}, "dest": {"type": "STRING"}, "root": {"type": "STRING"}, "pattern": {"type": "STRING"}, "verbal_confirmed": {"type": "BOOLEAN", "description": "Required for delete/empty_recycle_bin/format. Set true only after the user has clearly said yes out loud to a spoken confirmation."}}, "required": ["action"]}},
    {"name": "knowledge_action", "behavior": "NON_BLOCKING", "description": "Semantic search and instant file opening for indexed documents, files, PDFs, spreadsheets, notes, and folders (e.g., 'open trigonometric functions latest pdf', 'find the physics notes'). Actions: search, find_related, open (path — opens any file/document by exact path or natural title/query), index_now (folders — index/refresh folder(s) in background), reindex, stats.",
     "parameters": {"type": "OBJECT", "properties": {
         "action": {"type": "STRING", "description": "search, find_related, open, index_now, reindex, or stats"},
         "query": {"type": "STRING", "description": "Natural-language search query (for search)"},
         "path": {"type": "STRING", "description": "File path or natural title/query (for open / find_related), or a single folder to index/reindex"},
         "ext": {"type": "STRING", "description": "Optional file extension filter, e.g. 'pdf'"},
         "project": {"type": "STRING", "description": "Optional project root path filter"},
         "category": {"type": "STRING", "description": "Optional category filter, e.g. 'Study', 'Code', 'Images'"},
         "folders": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Any folder(s) to index/re-index — keyword ('desktop','downloads',...) or full path. Defaults to Desktop/Documents/Downloads if omitted."},
         "force": {"type": "BOOLEAN", "description": "For index_now: force re-embed every file even if unchanged. Always true for action='reindex'."}
}, "required": ["action"]}},
    {"name": "weather_action", "behavior": "NON_BLOCKING", "description": "Get current weather or forecast for a city. Leave city empty to use Vineet's home location (Orai, Uttar Pradesh, India, 285001).",
     "parameters": {"type": "OBJECT", "properties": {"city": {"type": "STRING"}, "forecast": {"type": "BOOLEAN"}}, "required": []}},
    {"name": "self_awareness", "behavior": "NON_BLOCKING", "description": "Introspect and (carefully) edit Gama's OWN source code/project — NOT the user's personal files (use file_controller/file_processor for those). Use whenever Sir asks what Gama is, how it works, what files/modules make it up, what it can do, or asks Gama to read/change/customize one of its own files. Actions: about (plain-English self-description), architecture (directory-by-directory map of how Gama is built), capabilities (live list of every tool Gama can currently call), list_files (path — list Gama's own project tree, empty path = root), read_file (path — read one of Gama's own source files), search (query — grep Gama's own codebase for a function/string/symbol, use this BEFORE edit_file to get the exact text to match), edit_file (path, find, replace — find-and-replace edit inside one of Gama's own files; find must match the file exactly once; always takes a backup first), create_file (path, content — create a brand-new file inside Gama's own project, e.g. a new actions/*.py module), revert_file (path — restore the last backup for a file Gama edited). edit_file/create_file change Gama's own source on disk and only take effect after Gama is restarted (restart_self) — say so. These are MEDIUM/HIGH risk and go through the normal confirmation flow.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING"}, "path": {"type": "STRING"}, "query": {"type": "STRING"}, "find": {"type": "STRING"}, "replace": {"type": "STRING"}, "content": {"type": "STRING"}}, "required": ["action"]}},
    {"name": "protocol_engine", "behavior": "NON_BLOCKING", "description": "JARVIS-style custom Protocols — user-defined multi-step routines triggered by a short numbered or named phrase, e.g. 'execute protocol 17', 'start protocol 17', 'run protocol alpha'. A protocol bundles together whatever the user wants (open apps, wait, open a URL, run another protocol/routine) under one quick trigger they define once. Actions: create (identifier — the number or name, e.g. '17' or 'Alpha'; steps — natural-language description of what it should do, e.g. 'open Chrome, then open Spotify, then wait 2 seconds'), run (identifier — executes it immediately; also handled instantly offline without a tool call for the common 'execute/start/run protocol N' phrasing, but call this yourself if the user phrases it differently, e.g. 'engage protocol 17' or 'kick off protocol alpha'), delete (identifier), list (no args — lists only configured protocols). Use this whenever the user says 'protocol' with a number or name — for creating one ('create/set up/make protocol 5 that opens...'), running one, listing them, or deleting one. Do NOT use for one-off unnamed automation (use computer_agent/computer_agent for that) — protocol_engine is specifically for the user's own named, reusable triggers. CRITICAL for create: the 'steps' argument must come from what the USER actually said the protocol should do. If the user only said something like 'create protocol 2' without describing any steps, do NOT invent, guess, or default to plausible-sounding steps yourself — instead reply asking what Protocol N should do (e.g. 'What should Protocol 2 do, Sir?') and wait for their answer; only call this tool with action='create' once they've told you the steps.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING", "description": "create, run, delete, or list"}, "identifier": {"type": "STRING", "description": "The protocol's number or name, e.g. '17' or 'Alpha'. Not needed for list."}, "steps": {"type": "STRING", "description": "For create only — natural-language description of the steps, taken from what the user actually said. Never fabricate a plausible-sounding value here; if the user hasn't described the steps yet, ask them instead of calling this tool."}, "description": {"type": "STRING", "description": "Optional human-readable description of what the protocol is for."}}, "required": ["action"]}},
    {"name": "reminder", "behavior": "NON_BLOCKING", "description": "Reminders, alarms, and timers. Actions: set (reminder in X min), alarm (at specific time), timer (countdown), list, cancel, cancel_all.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING", "description": "set, alarm, timer, list, cancel, cancel_all"}, "message": {"type": "STRING"}, "in_minutes": {"type": "INTEGER"}, "time": {"type": "STRING", "description": "For alarms: '7:30 AM' or '14:30'"}, "minutes": {"type": "INTEGER"}, "seconds": {"type": "INTEGER"}, "id": {"type": "INTEGER"}, "type": {"type": "STRING", "description": "reminder, alarm, or timer (for cancel)"}}, "required": ["action"]}},
    {"name": "set_confirmation_code", "description": "Set a confirmation code (4+ alphanumeric characters, no upper limit) required for destructive actions like shutdown/restart. User must set this before those actions work.",
     "parameters": {"type": "OBJECT", "properties": {"code": {"type": "STRING", "description": "4+ character alphanumeric code"}}, "required": ["code"]}},
    {"name": "notes", "behavior": "NON_BLOCKING", "description": "Create, read, list, delete, or append to notes. Notes are saved in Documents/GamaNotes/.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING", "description": "create, read, list, delete, append"}, "name": {"type": "STRING"}, "content": {"type": "STRING"}}, "required": ["action"]}},
    {"name": "system_info", "behavior": "NON_BLOCKING", "description": "Get detailed system information: overview, cpu, memory, disk, battery, network, or time. Use action='time' for ANY question about the current time, date, day, or 'how long until X' — it always returns the real Indian Standard Time (IST), never guess or compute time yourself.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING", "description": "overview, cpu, memory, disk, battery, network, time"}}, "required": []}},
    {"name": "clipboard", "behavior": "NON_BLOCKING", "description": "Read/write/clear clipboard, smart history, and AI pipeline. Actions: read, write (text=), clear, analyze, history, paste (index=), search (query=), summarize, translate (language=), fix_grammar, rewrite (write_back=true to replace clipboard), status.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING", "description": "read, write, clear, history, paste, search, clear_history, status"}, "text": {"type": "STRING"}, "index": {"type": "INTEGER"}, "query": {"type": "STRING"}, "limit": {"type": "INTEGER"}}, "required": ["action"]}},
    {"name": "file_find", "behavior": "NON_BLOCKING", "description": "Find and open local files by natural name/intent (Downloads/Documents/Desktop/active project). Actions: find (query=, type=pdf|doc|excel|image|code), open (query= or path=), recent. Prefer this for 'open the fee sheet pdf' / 'find yesterday's notes'.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING"}, "query": {"type": "STRING"}, "path": {"type": "STRING"}, "type": {"type": "STRING"}, "limit": {"type": "INTEGER"}}, "required": ["action"]}},
    {"name": "project_context", "behavior": "NON_BLOCKING", "description": "Track the user's active project and do-not-disturb. Actions: set (name=, path?, notes?), clear, list, status, dnd (minutes=), clear_dnd. Call set when user says they are working on a project.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING"}, "name": {"type": "STRING"}, "path": {"type": "STRING"}, "notes": {"type": "STRING"}, "minutes": {"type": "INTEGER"}, "reason": {"type": "STRING"}}, "required": ["action"]}},
    {"name": "display_stage", "behavior": "NON_BLOCKING", "description": "Control Gama Canvas (large visual panel on the RIGHT of the HUD, not the chat log). Use for: show my goals/tasks/reminders/alerts, show weather, show system status, show a timer (countdown), show a live clock/current time, show an image (URL or path), clear the screen, or a custom HUD. For 'what time is it' / 'show the time' / 'display the clock' use action=clock or action=time (live updating clock). For countdown timers use action=timer with minutes/seconds. action=show with scene_type is preferred. action=clear empties the canvas. action=custom_svg needs elements_json as a JSON string of SVG primitives (no JavaScript). action=image needs image set to a URL or local path. Keep responses short; never invent JSX/HTML.",
     "parameters": {"type": "OBJECT", "properties": {
         "action": {"type": "STRING", "description": "show, clear, weather, forecast, reminders, alerts, goals, tasks, timer, clock, time, system, information, custom_svg, image, compose, write, confirm, close, move, resize, save_layout, load_layout, list_layouts"},
         "scene_type": {"type": "STRING", "description": "For action=show: weather, tasks, goals, reminders, alerts, timer, clock, time, system, information, list, chart, progress, custom_svg, image"},
         "scene_id": {"type": "STRING"},
         "title": {"type": "STRING"},
         "content": {"type": "STRING"},
         "body": {"type": "STRING"},
         "message": {"type": "STRING"},
         "text": {"type": "STRING"},
         "location": {"type": "STRING"},
         "temperature": {"type": "NUMBER"},
         "condition": {"type": "STRING"},
         "city": {"type": "STRING"},
         "forecast": {"type": "BOOLEAN"},
         "cpu": {"type": "NUMBER"},
         "ram": {"type": "NUMBER"},
         "disk": {"type": "NUMBER"},
         "minutes": {"type": "NUMBER"},
         "seconds": {"type": "NUMBER"},
         "remaining_sec": {"type": "NUMBER"},
         "label": {"type": "STRING"},
         "layer": {"type": "NUMBER"},
         "duration": {"type": "NUMBER"},
         "image": {"type": "STRING", "description": "https URL or local file path"},
         "image_url": {"type": "STRING"},
         "caption": {"type": "STRING"},
         "elements_json": {"type": "STRING", "description": "JSON string array of SVG elements for custom_svg, e.g. [{\"type\":\"circle\",\"cx\":500,\"cy\":300,\"r\":80,\"stroke\":\"#38bdf8\",\"fill\":\"none\"}]"},
         "viewBox": {"type": "STRING"},
         "children_json": {"type": "STRING", "description": "JSON string array of child scenes for compose"},
         "link": {"type": "STRING"},
         "x": {"type": "NUMBER", "description": "Horizontal position 0-1 for move"},
         "y": {"type": "NUMBER", "description": "Vertical position 0-1 for move"},
         "slot": {"type": "STRING", "description": "Named place for move: center, left, right, top, bottom, top-left, top-right, bottom-left, bottom-right"},
         "where": {"type": "STRING", "description": "Alias for slot"},
         "w": {"type": "NUMBER", "description": "Width 0-1 for resize"},
         "h": {"type": "NUMBER", "description": "Height 0-1 for resize"},
         "scale": {"type": "NUMBER"},
         "named": {"type": "STRING", "description": "small | medium | large | full"},
         "name": {"type": "STRING", "description": "Layout name for save_layout / load_layout"}
     }, "required": ["action"]}},
    {"name": "d2_mode", "behavior": "NON_BLOCKING", "description": "Control D2 — Gama's secondary lightweight card/orb interface (NOT Nexus, NOT H1, NOT a dashboard). ONLY call this when the user explicitly says 'switch to D2', 'enter D2', 'open D2', or 'D2 mode'. Do NOT call for 'H1', 'enter H1', 'open H1', or 'spatial workspace' — those are d2_mode. NEVER activate D2 automatically for news, tasks, charts, CPU, or research. Actions: enter, exit, show_tasks, show_reminders, show_news, visualize_cpu, visualize_ram, clear, status.",
     "parameters": {"type": "OBJECT", "properties": {
         "action": {"type": "STRING", "description": "enter, exit, show_tasks, show_reminders, show_news, visualize_cpu, visualize_ram, clear, status"},
         "value": {"type": "NUMBER", "description": "CPU/RAM percent for visualize_*"},
         "state": {"type": "STRING", "description": "idle, listening, thinking, working, displaying, error"}
     }, "required": ["action"]}},
    {"name": "canvas_visual", "behavior": "NON_BLOCKING", "description": "Generate premium JARVIS-style visuals on Gama Canvas. Use for custom HUD, radar, system monitor, waveform, or multi-panel boards beyond simple tasks/weather cards. Pass a rich prompt. Returns immediately; the canvas updates in the background. Never invent JSX/HTML.",
     "parameters": {"type": "OBJECT", "properties": {
         "action": {"type": "STRING", "description": "generate (default)"},
         "prompt": {"type": "STRING", "description": "What to design, e.g. circular system HUD with CPU RAM rings and status text"},
         "description": {"type": "STRING"},
         "text": {"type": "STRING"}
     }, "required": ["prompt"]}},
    {"name": "email_sender", "behavior": "NON_BLOCKING", "description": "Send and READ emails (SMTP + IMAP). Actions: send (to, subject, body), setup (email, password, provider), read/list/unread (limit, query), summarize (limit), read_one (message_id or index). Gmail needs an App Password.",
     "parameters": {"type": "OBJECT", "properties": {
         "action": {"type": "STRING", "description": "send, setup, read, list, unread, summarize, or read_one"},
         "to": {"type": "STRING", "description": "recipient email"},
         "subject": {"type": "STRING"},
         "body": {"type": "STRING"},
         "email": {"type": "STRING", "description": "your email (for setup)"},
         "password": {"type": "STRING", "description": "your app password (for setup)"},
         "provider": {"type": "STRING", "description": "gmail, outlook, yahoo"},
         "limit": {"type": "INTEGER"},
         "query": {"type": "STRING"},
         "message_id": {"type": "STRING"},
         "index": {"type": "INTEGER"}
     }, "required": ["action"]}},
    
    
    {"name": "process_manager", "behavior": "NON_BLOCKING", "description": "List, kill (verified), or gracefully close (WM_CLOSE, lets the app prompt to save) running processes/windows. close_window closes an ENTIRE top-level window (every tab it owns, if it's a browser) — do NOT use it for 'close youtube' / 'close that tab' / any single-browser-tab request, since a browser window's title only reflects the active tab and WM_CLOSE takes the whole window down. Use browser_control's close_tab action for that instead. kill/kill_all are destructive and require verbal_confirmed=true — ask 'are you sure?' first, then only set it true once the user clearly says yes out loud.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING", "description": "list, kill, top, close_window"}, "name_or_pid": {"type": "STRING", "description": "process name, window title, or PID"}, "filter": {"type": "STRING", "description": "filter processes by name (for list)"}, "pid": {"type": "INTEGER"}, "verbal_confirmed": {"type": "BOOLEAN", "description": "Required for kill/kill_all. Set true only after the user has clearly said yes out loud to a spoken confirmation."}}, "required": ["action"]}},
    {"name": "user_settings", "behavior": "NON_BLOCKING", "description": "Let the user tweak Gama's own behavior by voice. Actions: set_personality (trait=humor|professionality|honesty|talkativeness, level=any percentage 0-100, e.g. '10', '20%', '65') e.g. 'set humor to 70%' or 'set talkativeness to 20 percent' — pass the exact number the user said as a string in `level`, don't round it to low/medium/high. Legacy words 'low'/'medium'/'high' (→30/50/80%) are still accepted for backward compatibility but a specific number the user gives should always be passed through as-is; get_personality; wake_greeting (enabled=true/false) e.g. 'enable wake greetings' makes Gama speak a full greeting on wake instead of just saying it's awake (off by default); voice_verification (enabled=true/false) turns speaker voice verification for destructive actions (shutdown/restart/delete/etc) on or off — when off, a confirmation code is required instead (on by default); status shows current settings. Call this whenever the user asks to change how you behave, your personality, or your verification preferences.",
     "parameters": {"type": "OBJECT", "properties": {
         "action": {"type": "STRING"},
         "trait": {"type": "STRING"},
         "level": {"type": "STRING"},
         "enabled": {"type": "BOOLEAN"}
     }, "required": ["action"]}},
    {"name": "music_engine", "behavior": "NON_BLOCKING", "description": "Gama's dedicated Music Engine. Use for ALL natural-language music requests. Handles: 'play Believer', 'play Heat Waves on Spotify', 'play my coding playlist', 'pause music', 'resume music', 'next song', 'previous song', 'volume up', 'what's playing', 'shuffle', 'repeat'. It automatically picks the best source (local Music folder, Spotify Desktop, Spotify Web API, YouTube Music, YouTube) and starts playback without asking the user to press Play. For playback requests, pass the exact user command as the 'command' parameter.",
     "parameters": {"type": "OBJECT", "properties": {
         "command": {"type": "STRING", "description": "the exact music command the user said, e.g. 'play Believer by Imagine Dragons' or 'pause music'"}
     }, "required": ["command"]}},
    {"name": "browser_control", "behavior": "NON_BLOCKING", "description": "Autonomous browser control via Playwright, PLUS close_tab which acts on the user's REAL Edge window (same one edge_search drives). Opens a REAL visible browser (Edge/Chrome). The browser stays open between turns so you can do multi-step tasks. Actions: open, navigate, click, type, press_key, read, screenshot, scroll, go_back, go_forward, close, close_tab (query — matches an open tab by its title, e.g. 'youtube', and closes ONLY that tab, leaving the window and other tabs open). Does NOT perform web searches — use edge_search for any 'search for X' request (it always searches in Edge's own search box). IMPORTANT: for 'close youtube' / 'close that tab' / 'close the X tab' style requests, always use browser_control close_tab, NEVER process_manager close_window — close_window sends WM_CLOSE to the whole top-level window and takes every tab down with it, which is virtually never what the user means by 'close youtube'.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING", "description": "open, navigate, click, type, press_key, read, screenshot, scroll, go_back, go_forward, close, close_tab"}, "url": {"type": "STRING"}, "selector": {"type": "STRING", "description": "CSS selector"}, "text": {"type": "STRING"}, "key": {"type": "STRING", "description": "Enter, Tab, Escape, etc."}, "press_enter": {"type": "BOOLEAN"}, "visible": {"type": "BOOLEAN"}, "channel": {"type": "STRING", "description": "msedge or chrome"}, "direction": {"type": "STRING"}, "amount": {"type": "INTEGER"}, "max_chars": {"type": "INTEGER"}, "query": {"type": "STRING", "description": "For close_tab: text to match against open tabs' titles, e.g. 'youtube'."}}, "required": ["action"]}},
    {"name": "keyboard_actions", "behavior": "NON_BLOCKING", "description": "Type text and press keys in the ACTIVE window (any app — not just browser). Prefer computer_agent action=open_and_type when the user asks to open an app AND write/type something in one request (e.g. 'open Notepad and write about yourself') — that opens, focuses, and types in one call. Use keyboard_actions alone only when the target window is already open and focused. For action=type, pass the full text to write; long text is pasted via clipboard automatically.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING", "description": "type, press, hotkey, hold, copy, paste, cut, select_all, undo, redo, save, find, new_tab, close_tab, switch_window"}, "text": {"type": "STRING"}, "key": {"type": "STRING", "description": "enter, tab, esc, space, backspace, delete, up, down, left, right, f1-f12"}, "keys": {"type": "STRING", "description": "comma-separated keys for hotkey e.g. 'ctrl,s'"}, "interval": {"type": "NUMBER"}, "duration": {"type": "NUMBER"}}, "required": ["action"]}},
    {"name": "mouse_actions", "behavior": "NON_BLOCKING", "description": "Mouse cursor control: physically move the cursor on screen, click, double-click, right-click, scroll, drag. 'move' here means moving the mouse pointer to screen coordinates — NOT moving a file (use file_controller) and NOT rearranging a window (use advanced_automation window_arrange). Works on any active window.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING", "description": "move, move_relative, click, double_click, right_click, scroll, drag, position, screen_size"}, "x": {"type": "INTEGER"}, "y": {"type": "INTEGER"}, "dx": {"type": "INTEGER"}, "dy": {"type": "INTEGER"}, "button": {"type": "STRING", "description": "left, right, middle"}, "clicks": {"type": "INTEGER"}, "amount": {"type": "INTEGER"}, "duration": {"type": "NUMBER"}}, "required": ["action"]}},
    {"name": "computer_agent", "behavior": "NON_BLOCKING", "description": "Autonomous multi-step computer tasks with natural-language understanding — break a goal into steps, run them, verify each one, and recover from common failures instead of stopping at the first problem. Combines opening apps + typing + clicking + browser, using accessibility APIs first and vision as a fallback. PREFERRED for 'open Notepad/Word and write/type …' requests: use action=open_and_type with app='Notepad' and text set to the full content to write (do NOT call open_app + keyboard_actions separately). Also use for chained requests like 'Open VS Code, launch GAMA, open Terminal and Spotify' (action=open_multiple, apps=[...]), 'open Edge and search for X', or a free-form goal like 'clean up my Downloads folder' (action=natural_task, request='...'). If natural_task can't confidently map the request to concrete steps, it returns a clarifying question instead of guessing — relay that question to the user. Actions: open_and_search, open_and_type, browser_search_and_read, open_app_and_wait, open_multiple, natural_task, click_smart.",
     "parameters": {"type": "OBJECT", "properties": {
         "action": {"type": "STRING"},
         "app": {"type": "STRING"}, "apps": {"type": "ARRAY", "items": {"type": "STRING"}},
         "query": {"type": "STRING"}, "text": {"type": "STRING"},
         "press_enter": {"type": "BOOLEAN", "description": "Only for open_and_type / open_and_search. Default false. Set true only when the user wants Enter pressed after typing (e.g. search boxes), never for writing into Notepad/editors."}, "engine": {"type": "STRING"},
         "wait_seconds": {"type": "NUMBER"}, "request": {"type": "STRING"},
         "window": {"type": "STRING"}, "target": {"type": "STRING"},
         "description": {"type": "STRING"}
}, "required": ["action"]}},
    {"name": "calendar_action", "behavior": "NON_BLOCKING", "description": "Real Google Calendar integration — read and manage the user's actual calendar (not just Gama-created reminders) — with a local .ics file (storage/gama_calendar.ics) kept in sync as an offline mirror. Reads/creates/updates/deletes still work with no internet or no Google login: requires the one-time Google login automatically once reconnected (or immediately if already connected). Use for 'what's on my schedule', 'am I free at X', 'add a meeting', 'move/reschedule my Y', 'cancel my Z', 'sync my calendar'. For holiday questions ('is there a holiday this month', 'when's the next holiday', 'any holidays in August') ALWAYS use the 'holidays' action, never 'list' or 'today' — holidays live on a separate public holiday calendar, not the user's primary calendar. Resolve natural-language times ('tomorrow 3pm', 'next Monday 10am') into full ISO 8601 datetimes yourself using the current date/time before calling create/update/list/holidays — this tool does not parse natural language dates. The action field must be EXACTLY ONE of the following single words (never combine two with a slash or space): status (connection state + offline event count + last sync time), today (today's events), next (the very next event), list (events in a time range — time_min, time_max as ISO datetimes, max_results), holidays (holidays from the user's configured public holiday calendar — time_min/time_max optional, defaults to the current calendar month; max_results), create (title, start, end optional — defaults to 1hr, location, description, attendees as comma-separated emails), update (event_query — title or id-prefix to find it, plus any of title/start/end/location to change), delete (event_query), sync (force an immediate two-way sync with Google right now).",
     "parameters": {"type": "OBJECT", "properties": {
         "action": {"type": "STRING"}, "title": {"type": "STRING"},
         "start": {"type": "STRING", "description": "ISO 8601 datetime, e.g. 2026-07-22T15:00:00"},
         "end": {"type": "STRING", "description": "ISO 8601 datetime"},
         "location": {"type": "STRING"}, "description": {"type": "STRING"},
         "attendees": {"type": "STRING", "description": "comma-separated email addresses"},
         "event_query": {"type": "STRING", "description": "title text or event id-prefix to identify an existing event"},
         "time_min": {"type": "STRING"}, "time_max": {"type": "STRING"},
         "max_results": {"type": "INTEGER"}
}, "required": ["action"]}},
    {"name": "ui_automation", "behavior": "NON_BLOCKING", "description": "Direct Windows accessibility (UIA) automation — list, click, or type into on-screen controls by their accessible name instead of screen coordinates. Prefer computer_agent for actual tasks; use this directly only for inspection ('what buttons are on this window') or a precise click/type that computer_agent's higher-level actions don't cover. Actions: list, click, type, exists.",
     "parameters": {"type": "OBJECT", "properties": {
         "action": {"type": "STRING"}, "window_title": {"type": "STRING"},
         "text": {"type": "STRING"}, "value": {"type": "STRING"},
         "control_type": {"type": "STRING"}, "double": {"type": "BOOLEAN"}
}, "required": ["action"]}},
    {"name": "advanced_automation", "behavior": "NON_BLOCKING", "description": "Advanced desktop automation: window arrangement (snap/tile/cascade windows on screen — NOT moving files), batch file rename, temp cleanup, system cleanup, quick action modes (focus/gaming/work/movie/night). Use window_arrange when the user says 'arrange my windows', 'snap side by side', 'tile', 'cascade' — NOT for moving files (use file_controller) and NOT for moving the mouse cursor (use mouse_actions). Every action here is destructive-tier and requires verbal_confirmed=true — ask 'are you sure?' first, then only set it true once the user clearly says yes out loud; never set it true on the first call.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING", "description": "window_arrange, batch_rename, clear_temp, system_cleanup, quick_action"}, "layout": {"type": "STRING", "description": "halves, cascade, minimize_all, show_desktop"}, "folder": {"type": "STRING"}, "pattern": {"type": "STRING"}, "prefix": {"type": "STRING"}, "name": {"type": "STRING", "description": "clear_desktop, focus_mode, gaming_mode, work_mode, movie_mode, night_mode"}, "verbal_confirmed": {"type": "BOOLEAN", "description": "Required for every action here. Set true only after the user has clearly said yes out loud to a spoken confirmation."}}, "required": ["action"]}},
    {"name": "automation_engine", "behavior": "NON_BLOCKING", "description": "Goal-driven batch automation for things NOT already covered by a more specific tool — organizing a messy folder by file type (any folder EXCEPT ~/Downloads — use file_controller for that), compressing every image in a folder, extracting every zip in a folder, bulk-renaming screenshots, moving all files of a given extension from one folder to another (e.g. 'move all .pdf files from Downloads to Documents' — note: 'move' here means bulk file relocation, not mouse movement), archiving a project folder to zip, or closing every window except one named app. CONFLICT RULES: 'organize Downloads' → file_controller, NOT this tool. 'move file/folder' → file_controller for a single item, this tool for bulk/extension-based moves. 'move window/snap windows' → advanced_automation. Do NOT use this for anything file_controller, computer_settings, process_manager, advanced_automation, media_controller, or clipboard already handle. Destructive results (lock/shutdown/restart/sleep/hibernate) require confirmation_code AND verbal_confirmed — ask the user for their code if not provided, and ask 'are you sure?' before setting verbal_confirmed=true.",
     "parameters": {"type": "OBJECT", "properties": {
         "goal": {"type": "STRING", "description": "the user's goal in plain English, e.g. 'organize my desktop' or 'extract every zip in Downloads'"},
         "confirmation_code": {"type": "STRING", "description": "only needed if the goal implies lock/shutdown/restart/sleep/hibernate"},
         "verbal_confirmed": {"type": "BOOLEAN", "description": "only needed if the goal implies lock/shutdown/restart/sleep/hibernate. Set true only after the user has clearly said yes out loud to a spoken confirmation."}
}, "required": ["goal"]}},
    {"name": "terminal_command", "behavior": "NON_BLOCKING", "description": "Run terminal/shell commands, open a real visible terminal window, or open a coding project in VS Code and run a command in it. Output/exit code is always captured and verified. Destructive commands (mass delete, disk format, etc.) are refused — use computer_settings for shutdown/restart instead.",
     "parameters": {"type": "OBJECT", "properties": {
         "action": {"type": "STRING", "description": "run (capture output, hidden), open_window (visible terminal), run_in_workspace (open a project folder in VS Code + run a command in its terminal)"},
         "command": {"type": "STRING", "description": "the shell command to run"},
         "cwd": {"type": "STRING", "description": "working directory to run the command in"},
         "path": {"type": "STRING", "description": "project folder path (for run_in_workspace)"},
         "timeout": {"type": "INTEGER", "description": "seconds before a 'run' command is considered hung (default 30)"},
         "shell": {"type": "STRING", "description": "for open_window: wt, powershell, or cmd (default: auto-detect)"}
}, "required": ["action"]}},
    {"name": "desktop_context", "behavior": "NON_BLOCKING", "description": "ON-DEMAND desktop awareness (not in the system prompt). Returns active app/window, VS Code workspace, browser tab, clipboard, network, battery, recent downloads. ALWAYS call this when the user references 'this', 'continue working', 'what I'm doing', or you need current screen/app state. Prefer this over guessing.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING", "description": "status, active_window, clipboard, network, battery, downloads"}}, "required": []}},
    
    {"name": "generate_image", "behavior": "NON_BLOCKING",
     "description": (
         "Generate an AI image from a text prompt (Gemini image model, Pollinations fallback). "
         "Saves to Desktop and shows it on the Gama display stage as a movable/resizable card "
         "by default. Only open in the system image viewer when the user explicitly asks to open it "
         "(set open_file=true). Use for photos, illustrations, concept art, wallpapers, logos. "
         "For precise geometric 2D diagrams prefer generate_model (2d mode) instead. "
         "Enrich prompts with style, colour, mood, and lighting."
     ),
     "parameters": {"type": "OBJECT", "properties": {
         "prompt": {"type": "STRING", "description": "Detailed visual description: subject, style, colours, mood, lighting, composition."},
         "open_file": {"type": "BOOLEAN", "description": "If true, also open in the OS image viewer. Default false — image appears on the display stage only."},
         "show_on_canvas": {"type": "BOOLEAN", "description": "Show on display stage (default true)."},
         "width": {"type": "NUMBER", "description": "Optional width for fallback generator (default 1024)."},
         "height": {"type": "NUMBER", "description": "Optional height for fallback generator (default 1024)."}
}, "required": ["prompt"]}},

    {"name": "telegram_sender", "behavior": "NON_BLOCKING", "description": "Send Telegram messages, Live native-audio voice notes, or files via Bot API. Actions: send (message=), send_voice/voice (message= OR regarding=class_schedule with day=today|tomorrow|week|next — ALWAYS use regarding=class_schedule for timetable voice notes so real config times are used; never invent class times), schedule_voice (pre-synthesize Live audio now, send exactly at at= time or in_minutes= — for 'send voice message regarding X at Y'), list_scheduled, cancel_scheduled (id=), send_file, setup, status, test, enable_alerts, disable_alerts. For send_voice: takes 10-20s — say you are sending now, NEVER say sent/delivered until a later tool result confirms success.",
     "parameters": {"type": "OBJECT", "properties": {
         "action": {"type": "STRING", "description": "send, send_voice, voice, schedule_voice, list_scheduled, cancel_scheduled, send_file, setup, status, test, enable_alerts, disable_alerts"},
         "message": {"type": "STRING", "description": "Text to send or speak (omit when regarding=class_schedule)"},
         "text": {"type": "STRING", "description": "Alias for message"},
         "regarding": {"type": "STRING", "description": "class_schedule | schedule | classes — load real timetable instead of freeform message"},
         "day": {"type": "STRING", "description": "today | tomorrow | week | next | monday… (with regarding=class_schedule)"},
         "use_schedule": {"type": "BOOLEAN", "description": "If true, speak real class_schedule data"},
         "at": {"type": "STRING", "description": "For schedule_voice: time like '7:00 PM' or '19:00'"},
         "in_minutes": {"type": "NUMBER", "description": "For schedule_voice: minutes from now"},
         "caption": {"type": "STRING"},
         "path": {"type": "STRING"},
         "query": {"type": "STRING"},
         "id": {"type": "INTEGER", "description": "Scheduled voice id for cancel_scheduled"},
         "bot_token": {"type": "STRING"},
         "chat_id": {"type": "STRING"},
         "token": {"type": "STRING"},
         "voice_name": {"type": "STRING"}
     }, "required": ["action"]}},
    {"name": "notification_manager", "behavior": "NON_BLOCKING", "description": "Show a desktop notification or list/clear recent notifications. Invocation: 'notify me that …', 'show a notification', 'clear notifications'.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING", "description": "show, list, clear"}, "title": {"type": "STRING"}, "message": {"type": "STRING"}, "body": {"type": "STRING"}}, "required": ["action"]}},
    {"name": "desktop_notify", "behavior": "NON_BLOCKING", "description": "Quick one-shot Windows desktop toast notification. Invocation: 'pop up a notification saying …'.",
     "parameters": {"type": "OBJECT", "properties": {"title": {"type": "STRING"}, "message": {"type": "STRING"}}, "required": ["message"]}},
    {"name": "session_restore", "behavior": "NON_BLOCKING", "description": "Restore apps that were open in the last Gama session. Invocation: 'restore my session', 'open what I had before'.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING", "description": "restore, list, status"}}, "required": []}},
    {"name": "remember", "behavior": "NON_BLOCKING", "description": "Store or update a durable personal fact. YOU decide what is worth keeping across sessions: identity, lasting preferences, project progress, relationships, constraints. Call silently when the user reveals something stable/personal. Do NOT store ephemeral commands ('open chrome', 'what time is it'). Prefer one concise factual sentence. For active-project progress, include the project name in the fact.",
     "parameters": {"type": "OBJECT", "properties": {"fact": {"type": "STRING"}, "text": {"type": "STRING"}, "content": {"type": "STRING"}, "project": {"type": "STRING"}}, "required": []}},
    {"name": "recall_memory", "behavior": "NON_BLOCKING", "description": "ON-DEMAND long-term memory (not stuffed into the system prompt). Search past facts/preferences. Call this whenever you need remembered details instead of inventing them. Invocation: 'what do you remember about …', 'do I prefer …'.",
     "parameters": {"type": "OBJECT", "properties": {"query": {"type": "STRING"}}, "required": ["query"]}},
    {"name": "media_controller", "behavior": "NON_BLOCKING", "description": "Control whatever media is playing (Spotify, VLC, browser, etc.): play, pause, next, previous, volume, what's playing. Invocation: 'pause', 'next song', 'what's playing'.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING", "description": "play, pause, next, previous, volume, status"}, "value": {"type": "STRING"}}, "required": ["action"]}},
    {"name": "utilities", "behavior": "NON_BLOCKING", "description": "Everyday helpers: translate, convert units, currency, calculate, define, spell, joke, quote, fact. Invocation: 'translate …', 'convert 10 km to miles', 'calculate …'.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING"}, "text": {"type": "STRING"}, "query": {"type": "STRING"}, "from_unit": {"type": "STRING"}, "to_unit": {"type": "STRING"}, "amount": {"type": "STRING"}}, "required": ["action"]}},
    {"name": "class_schedule", "behavior": "NON_BLOCKING", "description": "Look up Vineet's real Physics Wallah class timetable from config (12-hour times). Actions: today, tomorrow, week, next, set_day. ALWAYS call this (or telegram regarding=class_schedule) instead of inventing class times. Invocation: 'what's my next class', 'tomorrow's schedule', 'class schedule today'.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING", "description": "today, next, list"}, "day": {"type": "STRING"}}, "required": []}},
    {"name": "startup_manager", "description": "List/add/remove apps that run at Windows startup. Destructive add/remove requires confirmation when configured.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING", "description": "list, add, remove, enable, disable"}, "app": {"type": "STRING"}, "path": {"type": "STRING"}}, "required": ["action"]}},
    
    {"name": "webcam_process", "behavior": "NON_BLOCKING", "description": "Analyze webcam frame with vision.",
     "parameters": {"type": "OBJECT", "properties": {"prompt": {"type": "STRING"}}, "required": ["prompt"]}},
    {"name": "file_processor", "behavior": "NON_BLOCKING", "description": "Process files: images, PDFs, code, docs via Gemini vision.",
     "parameters": {"type": "OBJECT", "properties": {"path": {"type": "STRING"}, "action": {"type": "STRING"}, "instruction": {"type": "STRING"}}, "required": ["path"]}},
    {"name": "goal_tracker", "behavior": "NON_BLOCKING", "description": "Long-horizon goal tracking — for things that span days/weeks, unlike reminder (single-turn) or task_queue (one batch job). GAMA proactively checks in on active goals that are stale or nearing their deadline. Actions: create (title, description, deadline — natural language like 'in 2 weeks' or an ISO date), update (id, note, progress_pct 0-100), checkin (id — mark manually reviewed, resets the nag timer without changing progress), list (status: active|paused|done|abandoned|all), complete (id), pause (id), resume (id), abandon (id), history (id — recent update log).",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING"}, "id": {"type": "INTEGER"}, "title": {"type": "STRING"}, "description": {"type": "STRING"}, "deadline": {"type": "STRING"}, "note": {"type": "STRING"}, "progress_pct": {"type": "INTEGER"}, "status": {"type": "STRING"}}, "required": ["action"]}},
    {"name": "system_status", "behavior": "NON_BLOCKING", "description": "Get current CPU and RAM metrics only.",
     "parameters": {"type": "OBJECT", "properties": {}}},
    {"name": "save_memory", "behavior": "NON_BLOCKING", "description": "Structured save for stable profile fields. Call silently when YOU judge the info is personal and durable (language, name, home city, long-term preference). Language values: 'English', 'Hindi', or 'Hinglish'. Skip one-off commands. Prefer remember for free-form facts; use save_memory for key/value profile fields.",
     "parameters": {"type": "OBJECT", "properties": {"category": {"type": "STRING"}, "key": {"type": "STRING"}, "value": {"type": "STRING"}}, "required": ["category", "key", "value"]}},
    {"name": "forget_memory", "behavior": "NON_BLOCKING", "description": "Delete a specific long-term memory. Use when the user explicitly asks Gama to forget something, or says stored info is no longer true and should be removed entirely (rather than corrected — use 'remember' for corrections, since it updates in place).",
     "parameters": {"type": "OBJECT", "properties": {
         "query": {"type": "STRING", "description": "Description of the memory to forget, e.g. 'my old phone number' or 'the thing about the Berlin trip'."},
         "project": {"type": "STRING", "description": "Optional project name to scope the search to."},
     }, "required": ["query"]}},
    {"name": "credential_status", "behavior": "NON_BLOCKING", "description": "Report which secrets (API keys, tokens) are held in Gama's encrypted credential store, and whether secure storage is active. NEVER returns the actual secret values — names/status only. Use when the user asks whether their API keys are secure, or what's stored.",
     "parameters": {"type": "OBJECT", "properties": {}}}
]

# ---------------------------------------------------------------------------
# Perf audit item #2 — filter tool declarations by activity
# ---------------------------------------------------------------------------
# Gemini Live pays the schema cost at connection time and carries it through
# every turn.  Keep the first connection deliberately small (~20 tools): it
# covers everyday actions while avoiding the full schema prompt tax
# (currently ~70+ tools). Once a category is used, reconnect filtering can
# add that category's tools.

# ~15–20 everyday tools for the first Gemini Live connection.
# Volume/brightness/time/open-app/media are also handled by local fast-intent
# (core/fast_intent.py) so they never need a Gemini round-trip when matched.
# Categories beyond this set load on demand via reconnect filtering.
CORE_INITIAL_TOOLS: frozenset[str] = frozenset({
    "open_app",
    "reminder",
    "class_schedule",
    "desktop_context",
    "user_settings",
    "clipboard",
    "system_info",
    "system_status",
    "d2_mode",
    "media_controller",
    "computer_settings",   # volume, brightness, mute, sleep PC
    "edge_search",
    "goal_tracker",
    "display_stage",
    "file_find",
    "computer_agent",
    "recall_memory",
    "remember",
    "save_memory",
    "forget_memory",
    "telegram_sender",
})

ALWAYS_TOOLS: frozenset[str] = frozenset({
    "open_app", "system_status", "reminder", "notes", "display_stage", "d2_mode",
    "recall_memory", "save_memory", "remember", "forget_memory",
    "desktop_context", "set_confirmation_code", "credential_status",
    "session_restore", "user_settings", "clipboard", "system_info",
    "utilities", "shutdown_assistant",
    "set_voice", "goal_tracker", "file_find", "project_context",
    "desktop_notify", "notification_manager",
    "computer_agent",
    "live_vision", "edith_screen_vision",
    "self_awareness", "startup_manager",
})

TOOL_CATEGORIES: dict[str, frozenset[str]] = {
    "terminal_command":     frozenset({"coding", "system"}),
    "file_controller":      frozenset({"files"}),
    "file_find":            frozenset({"files"}),
    "file_processor":       frozenset({"files", "productivity"}),
    "canvas_visual":        frozenset({"vision", "display"}),
    "knowledge_action":     frozenset({"coding", "files"}),
    "project_context":      frozenset({"productivity"}),
    "telegram_sender":      frozenset({"communication"}),
    "clipboard":            frozenset({"utilities"}),
    "edge_search":          frozenset({"browsing"}),
    "browser_control":      frozenset({"browsing"}),
    "screen_agent":         frozenset({"browsing", "vision", "automation"}),
    "ui_automation":        frozenset({"browsing", "automation"}),
    "mouse_actions":        frozenset({"automation"}),
    "keyboard_actions":     frozenset({"automation"}),
    "advanced_automation":  frozenset({"automation", "system"}),
    "automation_engine":    frozenset({"automation"}),
    "calendar_action":      frozenset({"calendar"}),
    "email_sender":         frozenset({"communication"}),
    "music_engine":         frozenset({"media"}),
    "generate_image":       frozenset({"media"}),
    "computer_settings":    frozenset({"system"}),
    "process_manager":      frozenset({"system"}),
    "weather_action":       frozenset({"weather"}),
    "protocol_engine":      frozenset({"automation"}),
    "live_vision":          frozenset({"vision"}),
    "webcam_process":       frozenset({"vision"}),
    "edith_screen_vision":  frozenset({"vision"}),
}

def get_filtered_declarations(active_categories: "set[str] | frozenset[str] | None" = None) -> list[dict]:
    """Return the tool schema list to send for this connection.

    `active_categories=None` (or empty) → the compact initial core set.

    Otherwise → the core set plus tools whose category tags intersect
    `active_categories`. This keeps the current capability surface while
    avoiding unrelated tool schemas on future connections.
    """
    active = frozenset(active_categories or ())
    base = []
    for decl in TOOL_DECLARATIONS:
        name = decl["name"]
        if name in CORE_INITIAL_TOOLS or name in ALWAYS_TOOLS:
            base.append(decl)
            continue
        tags = TOOL_CATEGORIES.get(name)
        if tags is not None and tags & active:
            base.append(decl)
    # Phase 2: always attach drop-in plugin declarations
    try:
        from core.plugin_loader import get_plugin_declarations
        base.extend(get_plugin_declarations())
    except Exception:
        pass
    return base

# Phase 2 — merge plugin declarations when building the live tool list
def get_declarations_with_plugins(base_declarations=None):
    """Return tool declarations plus any drop-in plugins."""
    decls = list(base_declarations or [])
    try:
        from core.plugin_loader import get_plugin_declarations
        decls.extend(get_plugin_declarations())
    except Exception:
        pass
    return decls
