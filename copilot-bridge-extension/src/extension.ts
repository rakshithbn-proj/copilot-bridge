import * as vscode from 'vscode';
import * as http from 'http';
import * as fs from 'fs';
import * as path from 'path';
import * as https from 'https';
import * as url from 'url';
import * as os from 'os';
import * as crypto from 'crypto';
import { execFile } from 'child_process';

let server: http.Server | undefined;
let statusBarItem: vscode.StatusBarItem;
let extVersion = '5.2.2';
const MAX_BODY_SIZE = 10 * 1024 * 1024; // 10MB

// ==================== AUTH ====================

const CONFIG_DIR = path.join(os.homedir(), '.copilot-bridge');
const CONFIG_FILE = path.join(CONFIG_DIR, 'config.json');

function loadOrCreateApiKey(): string {
    try {
        if (fs.existsSync(CONFIG_FILE)) {
            const cfg = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'));
            if (cfg.apiKey && typeof cfg.apiKey === 'string' && cfg.apiKey.length >= 32) {
                return cfg.apiKey;
            }
        }
    } catch {
        // Fall through to generate a new key
    }

    // Generate a new key and persist it
    const apiKey = crypto.randomBytes(32).toString('hex');
    fs.mkdirSync(CONFIG_DIR, { recursive: true });
    fs.writeFileSync(CONFIG_FILE, JSON.stringify({ apiKey }, null, 2), { mode: 0o600 });
    return apiKey;
}

let _apiKey: string | undefined;

function getApiKey(): string {
    if (!_apiKey) {
        _apiKey = loadOrCreateApiKey();
    }
    return _apiKey;
}

function checkAuth(req: http.IncomingMessage): boolean {
    const header = req.headers['authorization'] || '';
    const token = header.startsWith('Bearer ') ? header.slice(7) : '';
    // timingSafeEqual prevents timing attacks; pad to equal length first
    const a = Buffer.alloc(64);
    const b = Buffer.alloc(64);
    Buffer.from(token).copy(a);
    Buffer.from(getApiKey()).copy(b);
    return crypto.timingSafeEqual(a, b);
}

// Active operations that can be cancelled
const activeCancellations = new Map<string, vscode.CancellationTokenSource>();
// Terminal output buffers (ring buffer per terminal)
const terminalOutputBuffers = new Map<string, string[]>();
const TERMINAL_BUFFER_MAX_LINES = 1000;
let terminalDataListener: vscode.Disposable | undefined;

// ==================== INTERFACES ====================

interface ChatRequest {
    messages: Array<{role: 'user' | 'assistant'; content: string}>;
    model?: string;
    maxTokens?: number;
    systemPrompt?: string;
    temperature?: number;
    topP?: number;
}

interface FileRequest {
    path: string;
    content?: string;
    startLine?: number;
    endLine?: number;
}

interface EditRequest {
    path: string;
    oldString: string;
    newString: string;
}

interface MultiEditRequest {
    edits: Array<{path: string; oldString: string; newString: string}>;
}

interface SearchRequest {
    pattern: string;
    directory?: string;
    filePattern?: string;
    maxResults?: number;
}

interface CommandRequest {
    command: string;
    cwd?: string;
    timeout?: number;
}

interface SymbolSearchRequest {
    query: string;
    kind?: string;
}

interface UsagesRequest {
    path: string;
    line: number;
    character: number;
}

interface FetchRequest {
    url: string;
    method?: string;
    headers?: Record<string, string>;
    body?: string;
}

interface GitRequest {
    staged?: boolean;
    includeUntracked?: boolean;
}

// ==================== EXTENSION ACTIVATION ====================

export function activate(context: vscode.ExtensionContext) {
    extVersion = context.extension.packageJSON.version || '4.1.0';
    console.log(`Copilot Bridge v${extVersion} (Full Feature) activating...`);

    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    statusBarItem.command = 'copilotBridge.status';
    context.subscriptions.push(statusBarItem);

    context.subscriptions.push(
        vscode.commands.registerCommand('copilotBridge.start', startServer),
        vscode.commands.registerCommand('copilotBridge.stop', stopServer),
        vscode.commands.registerCommand('copilotBridge.status', showStatus)
    );

    const config = vscode.workspace.getConfiguration('copilotBridge');
    if (config.get<boolean>('autoStart', true)) {
        startServer();
    }

    // Set up file watcher for incremental index updates
    setupFileWatcher(context);

    // Terminal output capture via shell integration
    try {
        terminalDataListener = (vscode.window as any).onDidWriteTerminalData((event: any) => {
            const name = event.terminal.name;
            if (!terminalOutputBuffers.has(name)) {
                terminalOutputBuffers.set(name, []);
            }
            const buf = terminalOutputBuffers.get(name)!;
            buf.push(event.data);
            // Ring buffer: keep last N lines
            while (buf.length > TERMINAL_BUFFER_MAX_LINES) {
                buf.shift();
            }
        });
        context.subscriptions.push(terminalDataListener!);
    } catch {
        // onDidWriteTerminalData may not be available in all VS Code versions
        console.log('Copilot Bridge: Terminal output capture not available');
    }

    // Clean up terminal buffers when terminals close
    context.subscriptions.push(
        vscode.window.onDidCloseTerminal(terminal => {
            terminalOutputBuffers.delete(terminal.name);
        })
    );
}

// ==================== UTILITIES ====================

function getWorkspaceRoot(): string {
    const folders = vscode.workspace.workspaceFolders;
    return folders && folders.length > 0 ? folders[0].uri.fsPath : process.cwd();
}

function resolvePath(filePath: string): string {
    const root = getWorkspaceRoot();
    const resolved = path.isAbsolute(filePath) ? filePath : path.join(root, filePath);
    const normalized = path.resolve(resolved);
    // Allow absolute paths but prevent traversal outside workspace for relative paths
    if (!path.isAbsolute(filePath) && !normalized.startsWith(root)) {
        throw new Error(`Path traversal detected: ${filePath}`);
    }
    return normalized;
}

function sendJson(res: http.ServerResponse, statusCode: number, data: any) {
    res.writeHead(statusCode, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(data));
}

function parseBody(req: http.IncomingMessage): Promise<string> {
    return new Promise((resolve, reject) => {
        let body = '';
        let size = 0;
        req.on('data', chunk => {
            size += chunk.length;
            if (size > MAX_BODY_SIZE) {
                req.destroy();
                reject(new Error('Request body too large'));
                return;
            }
            body += chunk.toString();
        });
        req.on('end', () => resolve(body));
        req.on('error', reject);
    });
}

// ==================== SERVER ====================

let activePort: number | undefined;

function isPortAvailable(port: number): Promise<boolean> {
    return new Promise((resolve) => {
        const tester = http.createServer();
        tester.once('error', () => resolve(false));
        tester.once('listening', () => {
            tester.close(() => resolve(true));
        });
        tester.listen(port, '127.0.0.1');
    });
}

async function findAvailablePort(startPort: number, maxAttempts: number = 10): Promise<number> {
    for (let i = 0; i < maxAttempts; i++) {
        const port = startPort + i;
        if (await isPortAvailable(port)) {
            return port;
        }
        console.log(`Copilot Bridge: port ${port} is in use, trying ${port + 1}...`);
    }
    throw new Error(`No available port found in range ${startPort}-${startPort + maxAttempts - 1}`);
}

async function startServer() {
    if (server) {
        vscode.window.showInformationMessage(`Copilot Bridge server is already running on port ${activePort}`);
        return;
    }

    const config = vscode.workspace.getConfiguration('copilotBridge');
    const configuredPort = config.get<number>('port', 5150);

    let port: number;
    try {
        port = await findAvailablePort(configuredPort);
    } catch (err: any) {
        vscode.window.showErrorMessage(`Copilot Bridge: ${err.message}`);
        return;
    }

    server = http.createServer(async (req, res) => {
        // CORS
        res.setHeader('Access-Control-Allow-Origin', '*');
        res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
        res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');

        if (req.method === 'OPTIONS') {
            res.writeHead(200);
            res.end();
            return;
        }

        const urlPath = req.url || '';

        // /health is public so clients can discover the port; everything else requires auth
        if (urlPath !== '/health' && !checkAuth(req)) {
            sendJson(res, 401, { success: false, error: 'Unauthorized: missing or invalid API key' });
            return;
        }

        try {
            // Route handling
            if (req.method === 'GET') {
                await handleGetRoutes(urlPath, res);
            } else if (req.method === 'POST') {
                let body: string;
                try {
                    body = await parseBody(req);
                } catch (parseErr: any) {
                    sendJson(res, 413, { success: false, error: parseErr.message });
                    return;
                }
                await handlePostRoutes(urlPath, body, res);
            } else {
                sendJson(res, 405, { error: 'Method not allowed' });
            }
        } catch (error: any) {
            sendJson(res, 500, { success: false, error: error.message });
        }
    });

    // Disable server-side timeouts so long-running LLM requests aren't killed
    server.timeout = 0;
    server.requestTimeout = 0;
    server.headersTimeout = 0;
    server.keepAliveTimeout = 0;

    server.listen(port, '127.0.0.1', () => {
        activePort = port;
        // Ensure key is generated before first request
        getApiKey();
        console.log(`Copilot Bridge v${extVersion} running on http://127.0.0.1:${port}`);
        console.log(`Copilot Bridge: API key stored at ${CONFIG_FILE}`);
        statusBarItem.text = `$(plug) Bridge v${extVersion}: ${port}`;
        statusBarItem.tooltip = `Copilot Bridge v${extVersion} - Port ${port}` + (port !== configuredPort ? ` (configured: ${configuredPort})` : '');
        statusBarItem.show();
        const portNote = port !== configuredPort ? ` (port ${configuredPort} was in use)` : '';
        vscode.window.showInformationMessage(`Copilot Bridge v${extVersion} started on port ${port}${portNote}. API key: ${CONFIG_FILE}`);

        // Build workspace index in background after server starts
        setTimeout(() => buildWorkspaceIndex(), 1000);
    });

    server.on('error', (err: any) => {
        vscode.window.showErrorMessage(`Failed to start Copilot Bridge: ${err.message}`);
        server = undefined;
        activePort = undefined;
    });
}

// ==================== GET ROUTES ====================

async function handleGetRoutes(urlPath: string, res: http.ServerResponse) {
    switch (urlPath) {
        case '/echo':
            // No-op: returns immediately without calling any VS Code API.
            // Used to isolate extension-host round-trip overhead from LLM latency.
            sendJson(res, 200, { success: true, echo: true, timestamp: Date.now() });
            break;

        case '/health':
            sendJson(res, 200, { 
                status: 'ok', 
                version: extVersion,
                port: activePort,
                features: [
                    'chat', 'chat/stream', 'chat/image', 'files', 'search', 'commands', 'git',
                    'git/add', 'git/push', 'git/pull', 'git/merge',
                    'diagnostics', 'symbols', 'usages', 'fetch', 'vscode',
                    'hover', 'codeActions', 'documentSymbols', 'rename',
                    'callHierarchy', 'terminal', 'terminal/output', 'streaming',
                    'cancel', 'tokens/count', 'undo-support',
                    'semantic-search', 'workspace-index', 'import-graph', 'related-files',
                    'lm/tools', 'workspace/trust', 'copilot/instructions'
                ]
            });
            break;

        case '/models':
            const models = await vscode.lm.selectChatModels({});
            sendJson(res, 200, {
                models: models.map(m => ({
                    id: m.id, name: m.name, vendor: m.vendor,
                    family: m.family, version: m.version,
                    maxInputTokens: m.maxInputTokens,
                    capabilities: (m as any).capabilities || undefined
                }))
            });
            break;

        case '/lm/tools':
            const lmTools = (vscode.lm as any).tools || [];
            sendJson(res, 200, {
                success: true,
                tools: lmTools.map((t: any) => ({
                    name: t.name,
                    description: t.description || '',
                    tags: t.tags || [],
                    inputSchema: t.inputSchema || {}
                }))
            });
            break;

        case '/workspace/trust':
            sendJson(res, 200, {
                success: true,
                trusted: vscode.workspace.isTrusted,
                trustLevel: vscode.workspace.isTrusted ? 'full' : 'restricted'
            });
            break;

        case '/copilot/instructions':
            await handleCopilotInstructions(res);
            break;

        case '/workspace':
            const folders = vscode.workspace.workspaceFolders || [];
            sendJson(res, 200, {
                success: true,
                root: getWorkspaceRoot(),
                folders: folders.map(f => ({ name: f.name, path: f.uri.fsPath })),
                name: vscode.workspace.name
            });
            break;

        case '/diagnostics':
        case '/errors':
            const allDiagnostics: any[] = [];
            vscode.languages.getDiagnostics().forEach(([uri, diagnostics]) => {
                diagnostics.forEach(d => {
                    allDiagnostics.push({
                        file: vscode.workspace.asRelativePath(uri),
                        line: d.range.start.line + 1,
                        character: d.range.start.character + 1,
                        message: d.message,
                        severity: ['Error', 'Warning', 'Info', 'Hint'][d.severity],
                        source: d.source
                    });
                });
            });
            sendJson(res, 200, { success: true, diagnostics: allDiagnostics });
            break;

        case '/terminals':
            const terminals = vscode.window.terminals.map(t => ({
                name: t.name,
                processId: t.processId
            }));
            sendJson(res, 200, { success: true, terminals });
            break;

        case '/extensions':
            const extensions = vscode.extensions.all
                .filter(e => !e.id.startsWith('vscode.'))
                .map(e => ({
                    id: e.id,
                    name: e.packageJSON.displayName || e.id,
                    active: e.isActive
                }));
            sendJson(res, 200, { success: true, extensions });
            break;

        case '/editor':
            const editor = vscode.window.activeTextEditor;
            if (editor) {
                sendJson(res, 200, {
                    success: true,
                    file: vscode.workspace.asRelativePath(editor.document.uri),
                    language: editor.document.languageId,
                    lineCount: editor.document.lineCount,
                    selection: {
                        start: { line: editor.selection.start.line + 1, character: editor.selection.start.character },
                        end: { line: editor.selection.end.line + 1, character: editor.selection.end.character }
                    },
                    selectedText: editor.document.getText(editor.selection)
                });
            } else {
                sendJson(res, 200, { success: true, file: null });
            }
            break;

        case '/workspace/index':
            handleWorkspaceIndexInfo(res);
            break;

        case '/workspace/index/files':
            handleWorkspaceIndexFiles(res);
            break;

        default:
            sendJson(res, 404, { error: 'Not found' });
    }
}

// ==================== POST ROUTES ====================

async function handlePostRoutes(urlPath: string, body: string, res: http.ServerResponse) {
    let data: any;
    try {
        data = body ? JSON.parse(body) : {};
    } catch {
        sendJson(res, 400, { success: false, error: 'Invalid JSON body' });
        return;
    }

    switch (urlPath) {
        // ===== CHAT =====
        case '/chat':
            const chatResponse = await handleChat(data as ChatRequest);
            sendJson(res, 200, chatResponse);
            break;

        case '/chat/stream':
            await handleChatStream(data as ChatRequest, res);
            break;

        case '/chat/image':
            await handleChatWithImage(data, res);
            break;

        // ===== FILE OPERATIONS =====
        case '/file/read':
            await handleFileRead(data as FileRequest, res);
            break;

        case '/file/write':
            await handleFileWrite(data as FileRequest, res);
            break;

        case '/file/edit':
            await handleFileEdit(data as EditRequest, res);
            break;

        case '/file/multi-edit':
            await handleMultiEdit(data as MultiEditRequest, res);
            break;

        case '/file/list':
            await handleFileList(data as FileRequest, res);
            break;

        case '/file/delete':
            await handleFileDelete(data as FileRequest, res);
            break;

        case '/file/rename':
            await handleFileRename(data, res);
            break;

        case '/file/copy':
            await handleFileCopy(data, res);
            break;

        // ===== SEARCH =====
        case '/search/text':
        case '/file/search':
            await handleTextSearch(data as SearchRequest, res);
            break;

        case '/search/files':
            await handleFileSearch(data, res);
            break;

        case '/search/symbols':
            await handleSymbolSearch(data as SymbolSearchRequest, res);
            break;

        case '/search/usages':
        case '/references':
            await handleFindUsages(data as UsagesRequest, res);
            break;

        case '/search/definition':
            await handleFindDefinition(data as UsagesRequest, res);
            break;

        // ===== CODE INTELLIGENCE (NEW) =====
        case '/search/hover':
            await handleHover(data as UsagesRequest, res);
            break;

        case '/search/codeActions':
            await handleCodeActions(data, res);
            break;

        case '/search/documentSymbols':
            await handleDocumentSymbols(data as FileRequest, res);
            break;

        case '/search/rename':
            await handleRenameSymbol(data, res);
            break;

        case '/search/callHierarchy':
            await handleCallHierarchy(data, res);
            break;

        // ===== SEMANTIC SEARCH + WORKSPACE INDEX =====
        case '/search/semantic':
            await handleSemanticSearch(data, res);
            break;

        case '/workspace/related':
            handleRelatedFiles(data, res);
            break;

        case '/workspace/imports':
            handleImportGraph(data, res);
            break;

        case '/workspace/reindex':
            await handleReindex(res);
            break;

        // ===== COMMANDS =====
        case '/command/run':
            await handleRunCommand(data as CommandRequest, res);
            break;

        case '/vscode/command':
            await handleVSCodeCommand(data, res);
            break;

        // ===== TERMINAL (NEW) =====
        case '/terminal/create':
            await handleTerminalCreate(data, res);
            break;

        case '/terminal/send':
            await handleTerminalSend(data, res);
            break;

        case '/terminal/dispose':
            await handleTerminalDispose(data, res);
            break;

        case '/terminal/output':
            await handleTerminalOutput(data, res);
            break;

        // ===== CANCELLATION + PROGRESS (NEW) =====
        case '/cancel':
            await handleCancel(data, res);
            break;

        case '/tokens/count':
            await handleTokenCount(data, res);
            break;

        // ===== GIT =====
        case '/git/status':
            await handleGitStatus(res);
            break;

        case '/git/diff':
            await handleGitDiff(data, res);
            break;

        case '/git/changed':
            await handleGitChangedFiles(data as GitRequest, res);
            break;

        case '/git/log':
            await handleGitLog(data, res);
            break;

        case '/git/branches':
            await handleGitBranches(res);
            break;

        // ===== GIT EXTENDED =====
        case '/git/commit':
            await handleGitCommit(data, res);
            break;

        case '/git/stash':
            await handleGitStash(data, res);
            break;

        case '/git/checkout':
            await handleGitCheckout(data, res);
            break;

        case '/git/add':
            await handleGitAdd(data, res);
            break;

        case '/git/push':
            await handleGitPush(data, res);
            break;

        case '/git/pull':
            await handleGitPull(data, res);
            break;

        case '/git/merge':
            await handleGitMerge(data, res);
            break;

        // ===== DIAGNOSTICS =====
        case '/diagnostics/file':
            await handleFileDiagnostics(data as FileRequest, res);
            break;

        // ===== NETWORK =====
        case '/fetch':
            await handleFetch(data as FetchRequest, res);
            break;

        // ===== EDITOR =====
        case '/editor/open':
            await handleOpenFile(data, res);
            break;

        case '/editor/insert':
            await handleInsertText(data, res);
            break;

        case '/editor/selection':
            await handleGetSelection(res);
            break;

        // ===== NOTIFICATIONS =====
        case '/notify/info':
            vscode.window.showInformationMessage(data.message || 'Info');
            sendJson(res, 200, { success: true });
            break;

        case '/notify/warn':
            vscode.window.showWarningMessage(data.message || 'Warning');
            sendJson(res, 200, { success: true });
            break;

        case '/notify/error':
            vscode.window.showErrorMessage(data.message || 'Error');
            sendJson(res, 200, { success: true });
            break;

        case '/notify/input':
            const input = await vscode.window.showInputBox({
                prompt: data.prompt,
                placeHolder: data.placeholder,
                value: data.defaultValue
            });
            sendJson(res, 200, { success: true, value: input });
            break;

        case '/notify/quickpick':
            const picked = await vscode.window.showQuickPick(data.items || [], {
                placeHolder: data.placeholder,
                canPickMany: data.multiSelect
            });
            sendJson(res, 200, { success: true, value: picked });
            break;

        default:
            sendJson(res, 404, { error: 'Not found' });
    }
}

// ==================== CHAT HANDLER ====================

async function handleChat(request: ChatRequest): Promise<any> {
    try {
        const models = await vscode.lm.selectChatModels({});
        if (models.length === 0) {
            return { success: false, error: 'No language models available' };
        }

        let selectedModel = models[0];
        if (request.model) {
            const match = models.find(m => 
                m.id.toLowerCase().includes(request.model!.toLowerCase()) ||
                m.name.toLowerCase().includes(request.model!.toLowerCase()) ||
                m.family.toLowerCase().includes(request.model!.toLowerCase())
            );
            if (match) selectedModel = match;
        }

        const messages: vscode.LanguageModelChatMessage[] = [];
        
        if (request.systemPrompt) {
            messages.push(vscode.LanguageModelChatMessage.User(
                `[System Instructions]\n${request.systemPrompt}\n[End System Instructions]\n\n`
            ));
        }

        for (const msg of request.messages) {
            if (msg.role === 'user') {
                messages.push(vscode.LanguageModelChatMessage.User(msg.content));
            } else {
                messages.push(vscode.LanguageModelChatMessage.Assistant(msg.content));
            }
        }

        const modelOptions: Record<string, any> = {};
        if (request.temperature !== undefined) { modelOptions.temperature = request.temperature; }
        if (request.topP !== undefined) { modelOptions.top_p = request.topP; }
        if (request.maxTokens !== undefined) { modelOptions.max_tokens = request.maxTokens; }

        const requestOptions: vscode.LanguageModelChatRequestOptions = {
            ...(Object.keys(modelOptions).length > 0 ? { modelOptions } : {})
        };

        const cts = new vscode.CancellationTokenSource();
        try {
            const response = await selectedModel.sendRequest(
                messages, requestOptions, cts.token
            );

            let fullResponse = '';
            for await (const chunk of response.text) {
                fullResponse += chunk;
            }

            return { success: true, content: fullResponse, model: selectedModel.id };
        } finally {
            cts.dispose();
        }
    } catch (error: any) {
        return { success: false, error: error.message };
    }
}

// ==================== FILE HANDLERS ====================

async function handleFileRead(request: FileRequest, res: http.ServerResponse) {
    const fullPath = resolvePath(request.path);
    
    if (!fs.existsSync(fullPath)) {
        sendJson(res, 404, { success: false, error: 'File not found' });
        return;
    }
    
    let content = fs.readFileSync(fullPath, 'utf-8');
    
    if (request.startLine || request.endLine) {
        const lines = content.split('\n');
        const start = (request.startLine || 1) - 1;
        const end = request.endLine || lines.length;
        content = lines.slice(start, end).join('\n');
    }
    
    sendJson(res, 200, { success: true, content, path: fullPath });
}

async function handleFileWrite(request: FileRequest, res: http.ServerResponse) {
    const fullPath = resolvePath(request.path);
    const uri = vscode.Uri.file(fullPath);
    const content = request.content || '';
    const contentBytes = new Uint8Array(Array.from(content).map(c => c.charCodeAt(0)));

    const exists = fs.existsSync(fullPath);

    if (!exists) {
        // Create parent directories, then create via WorkspaceEdit
        const dir = path.dirname(fullPath);
        if (!fs.existsSync(dir)) {
            fs.mkdirSync(dir, { recursive: true });
        }
        const wsEdit = new vscode.WorkspaceEdit();
        wsEdit.createFile(uri, { overwrite: true, contents: contentBytes });
        await vscode.workspace.applyEdit(wsEdit);
    } else {
        // For existing files: open document, replace all content via editor API (supports undo)
        const doc = await vscode.workspace.openTextDocument(uri);
        const fullRange = new vscode.Range(
            doc.positionAt(0),
            doc.positionAt(doc.getText().length)
        );
        const wsEdit = new vscode.WorkspaceEdit();
        wsEdit.replace(uri, fullRange, content);
        await vscode.workspace.applyEdit(wsEdit);
    }

    sendJson(res, 200, { success: true, path: fullPath });
}

async function handleFileEdit(request: EditRequest, res: http.ServerResponse) {
    const fullPath = resolvePath(request.path);
    const uri = vscode.Uri.file(fullPath);

    if (!fs.existsSync(fullPath)) {
        sendJson(res, 404, { success: false, error: 'File not found' });
        return;
    }

    // Open via VS Code so edits go through workspace edit (supports undo)
    const doc = await vscode.workspace.openTextDocument(uri);
    const text = doc.getText();

    const idx = text.indexOf(request.oldString);
    if (idx === -1) {
        sendJson(res, 400, { success: false, error: 'String not found in file' });
        return;
    }

    const startPos = doc.positionAt(idx);
    const endPos = doc.positionAt(idx + request.oldString.length);
    const range = new vscode.Range(startPos, endPos);

    const wsEdit = new vscode.WorkspaceEdit();
    wsEdit.replace(uri, range, request.newString);
    const applied = await vscode.workspace.applyEdit(wsEdit);

    sendJson(res, 200, { success: applied, path: fullPath });
}

async function handleMultiEdit(request: MultiEditRequest, res: http.ServerResponse) {
    const results: any[] = [];
    const wsEdit = new vscode.WorkspaceEdit();
    const editPaths: string[] = [];

    // Build a single WorkspaceEdit for atomicity
    for (const edit of request.edits) {
        try {
            const fullPath = resolvePath(edit.path);
            const uri = vscode.Uri.file(fullPath);
            const doc = await vscode.workspace.openTextDocument(uri);
            const text = doc.getText();

            const idx = text.indexOf(edit.oldString);
            if (idx === -1) {
                results.push({ path: edit.path, success: false, error: 'String not found' });
                continue;
            }

            const startPos = doc.positionAt(idx);
            const endPos = doc.positionAt(idx + edit.oldString.length);
            wsEdit.replace(uri, new vscode.Range(startPos, endPos), edit.newString);
            editPaths.push(edit.path);
            results.push({ path: edit.path, success: true });
        } catch (error: any) {
            results.push({ path: edit.path, success: false, error: error.message });
        }
    }

    if (editPaths.length > 0) {
        const applied = await vscode.workspace.applyEdit(wsEdit);
        if (!applied) {
            // Mark all as failed if atomic apply failed
            for (const r of results) {
                if (r.success) { r.success = false; r.error = 'Atomic apply failed'; }
            }
        }
    }

    sendJson(res, 200, { success: true, results });
}

async function handleFileList(request: FileRequest, res: http.ServerResponse) {
    const fullPath = resolvePath(request.path || '.');
    
    if (!fs.existsSync(fullPath)) {
        sendJson(res, 404, { success: false, error: 'Directory not found' });
        return;
    }
    
    const entries = fs.readdirSync(fullPath, { withFileTypes: true });
    const items = entries.map(e => ({
        name: e.name,
        isDirectory: e.isDirectory(),
        isFile: e.isFile(),
        path: path.join(fullPath, e.name)
    }));
    
    sendJson(res, 200, { success: true, items, path: fullPath });
}

async function handleFileDelete(request: FileRequest, res: http.ServerResponse) {
    const fullPath = resolvePath(request.path);
    
    if (!fs.existsSync(fullPath)) {
        sendJson(res, 404, { success: false, error: 'File/directory not found' });
        return;
    }
    
    const stat = fs.statSync(fullPath);
    if (stat.isDirectory()) {
        fs.rmSync(fullPath, { recursive: true, force: true });
    } else {
        fs.unlinkSync(fullPath);
    }
    
    sendJson(res, 200, { success: true, deleted: fullPath });
}

async function handleFileRename(data: any, res: http.ServerResponse) {
    const oldPath = resolvePath(data.oldPath);
    const newPath = resolvePath(data.newPath);
    
    if (!fs.existsSync(oldPath)) {
        sendJson(res, 404, { success: false, error: 'Source not found' });
        return;
    }
    
    fs.renameSync(oldPath, newPath);
    sendJson(res, 200, { success: true, oldPath, newPath });
}

async function handleFileCopy(data: any, res: http.ServerResponse) {
    const src = resolvePath(data.source);
    const dest = resolvePath(data.destination);
    
    if (!fs.existsSync(src)) {
        sendJson(res, 404, { success: false, error: 'Source not found' });
        return;
    }
    
    fs.copyFileSync(src, dest);
    sendJson(res, 200, { success: true, source: src, destination: dest });
}

// ==================== SEARCH HANDLERS ====================

async function handleTextSearch(request: SearchRequest, res: http.ServerResponse) {
    const results: any[] = [];
    const maxResults = request.maxResults || 100;

    // Use VS Code's built-in text search - respects .gitignore, much faster
    const searchPattern = {
        pattern: request.pattern,
        isRegExp: true,
        isCaseSensitive: false
    };

    const includePattern = request.filePattern || undefined;
    const exclude = '**/node_modules/**';

    try {
        await (vscode.workspace as any).findTextInFiles(searchPattern, {
            include: includePattern ? new vscode.RelativePattern(getWorkspaceRoot(), `**/*${includePattern}*`) : undefined,
            exclude: exclude,
            maxResults: maxResults
        }, (result: any) => {
            if (results.length < maxResults) {
                const ranges = Array.isArray(result.ranges) ? result.ranges : [result.ranges];
                for (const range of ranges) {
                    if (results.length >= maxResults) { break; }
                    results.push({
                        file: vscode.workspace.asRelativePath(result.uri),
                        line: (range.start?.line ?? range.startLineNumber ?? 0) + 1,
                        content: (result.preview?.text || '').substring(0, 300).trim()
                    });
                }
            }
        });
    } catch {
        // Fallback to simple fs scan if VS Code API fails (e.g., no workspace open)
        const searchDir = resolvePath(request.directory || '.');
        const regexPattern = new RegExp(request.pattern, 'gi');
        const filePattern = request.filePattern ? new RegExp(request.filePattern) : null;

        function searchInDir(dir: string, depth: number = 0) {
            if (depth > 10 || results.length >= maxResults) { return; }
            try {
                const entries = fs.readdirSync(dir, { withFileTypes: true });
                for (const entry of entries) {
                    if (results.length >= maxResults) { break; }
                    const fullPath = path.join(dir, entry.name);
                    if (entry.name.startsWith('.') ||
                        ['node_modules', '__pycache__', '.git', 'out', 'dist', 'build'].includes(entry.name)) {
                        continue;
                    }
                    if (entry.isDirectory()) {
                        searchInDir(fullPath, depth + 1);
                    } else if (entry.isFile()) {
                        if (filePattern && !filePattern.test(entry.name)) { continue; }
                        try {
                            const content = fs.readFileSync(fullPath, 'utf-8');
                            const lines = content.split('\n');
                            lines.forEach((line, idx) => {
                                if (results.length < maxResults && regexPattern.test(line)) {
                                    results.push({
                                        file: path.relative(getWorkspaceRoot(), fullPath),
                                        line: idx + 1,
                                        content: line.trim().substring(0, 300)
                                    });
                                }
                            });
                        } catch {}
                    }
                }
            } catch {}
        }
        searchInDir(searchDir);
    }

    sendJson(res, 200, { success: true, results });
}

async function handleFileSearch(data: any, res: http.ServerResponse) {
    const pattern = data.pattern || '*';
    const files = await vscode.workspace.findFiles(pattern, '**/node_modules/**', data.maxResults || 100);
    
    sendJson(res, 200, {
        success: true,
        files: files.map(f => vscode.workspace.asRelativePath(f))
    });
}

async function handleSymbolSearch(request: SymbolSearchRequest, res: http.ServerResponse) {
    const symbols = await vscode.commands.executeCommand<vscode.SymbolInformation[]>(
        'vscode.executeWorkspaceSymbolProvider', request.query
    );
    
    const results = (symbols || []).slice(0, 50).map(s => ({
        name: s.name,
        kind: vscode.SymbolKind[s.kind],
        file: vscode.workspace.asRelativePath(s.location.uri),
        line: s.location.range.start.line + 1
    }));
    
    sendJson(res, 200, { success: true, symbols: results });
}

async function handleFindUsages(request: UsagesRequest, res: http.ServerResponse) {
    const uri = vscode.Uri.file(resolvePath(request.path));
    const position = new vscode.Position(request.line - 1, request.character);
    
    const locations = await vscode.commands.executeCommand<vscode.Location[]>(
        'vscode.executeReferenceProvider', uri, position
    );
    
    const usages = (locations || []).map(loc => ({
        file: vscode.workspace.asRelativePath(loc.uri),
        line: loc.range.start.line + 1,
        character: loc.range.start.character
    }));
    
    sendJson(res, 200, { success: true, usages });
}

async function handleFindDefinition(request: UsagesRequest, res: http.ServerResponse) {
    const uri = vscode.Uri.file(resolvePath(request.path));
    const position = new vscode.Position(request.line - 1, request.character);
    
    const locations = await vscode.commands.executeCommand<vscode.Location[]>(
        'vscode.executeDefinitionProvider', uri, position
    );
    
    const definitions = (locations || []).map(loc => ({
        file: vscode.workspace.asRelativePath(loc.uri),
        line: loc.range.start.line + 1,
        character: loc.range.start.character
    }));
    
    sendJson(res, 200, { success: true, definitions });
}

// ==================== COMMAND HANDLERS ====================

async function handleRunCommand(request: CommandRequest, res: http.ServerResponse) {
    const cwd = request.cwd ? resolvePath(request.cwd) : getWorkspaceRoot();
    const timeout = request.timeout || 30000;
    
    const { exec } = require('child_process');
    
    exec(request.command, { cwd, timeout }, (error: any, stdout: string, stderr: string) => {
        sendJson(res, 200, {
            success: !error,
            stdout: stdout.substring(0, 100000),
            stderr: stderr.substring(0, 20000),
            exitCode: error ? error.code : 0
        });
    });
}

async function handleVSCodeCommand(data: any, res: http.ServerResponse) {
    try {
        const result = await vscode.commands.executeCommand(data.command, ...(data.args || []));
        sendJson(res, 200, { success: true, result: result !== undefined ? String(result) : null });
    } catch (error: any) {
        sendJson(res, 400, { success: false, error: error.message });
    }
}

// ==================== GIT HANDLERS ====================

async function handleGitStatus(res: http.ServerResponse) {
    execFile('git', ['status', '--porcelain'], { cwd: getWorkspaceRoot() }, (error: any, stdout: string) => {
        if (error) {
            sendJson(res, 200, { success: false, error: 'Not a git repository' });
            return;
        }
        
        const files = stdout.trim().split('\n').filter(Boolean).map(line => ({
            status: line.substring(0, 2).trim(),
            file: line.substring(3)
        }));
        
        sendJson(res, 200, { success: true, files });
    });
}

async function handleGitDiff(data: any, res: http.ServerResponse) {
    const args = ['diff'];
    if (data.staged) { args.push('--staged'); }
    if (data.file) { args.push('--', data.file); }
    
    execFile('git', args, { cwd: getWorkspaceRoot(), maxBuffer: 10 * 1024 * 1024 }, 
        (error: any, stdout: string) => {
            sendJson(res, 200, { success: true, diff: stdout.substring(0, 500000) });
        }
    );
}

async function handleGitChangedFiles(request: GitRequest, res: http.ServerResponse) {
    const args = request.staged 
        ? ['diff', '--name-only', '--staged'] 
        : ['diff', '--name-only', 'HEAD'];
    
    execFile('git', args, { cwd: getWorkspaceRoot() }, (error: any, stdout: string) => {
        let files = stdout.trim().split('\n').filter(Boolean);
        
        if (request.includeUntracked) {
            execFile('git', ['ls-files', '--others', '--exclude-standard'], { cwd: getWorkspaceRoot() }, 
                (err2: any, stdout2: string) => {
                    const untracked = stdout2.trim().split('\n').filter(Boolean);
                    files = [...new Set([...files, ...untracked])];
                    sendJson(res, 200, { success: true, files });
                }
            );
        } else {
            sendJson(res, 200, { success: true, files });
        }
    });
}

async function handleGitLog(data: any, res: http.ServerResponse) {
    const limit = Math.min(Math.max(1, data.limit || 20), 500);
    
    execFile('git', ['log', '--oneline', '-n', String(limit)], { cwd: getWorkspaceRoot() }, (error: any, stdout: string) => {
        const commits = stdout.trim().split('\n').filter(Boolean).map(line => {
            const [hash, ...messageParts] = line.split(' ');
            return { hash, message: messageParts.join(' ') };
        });
        sendJson(res, 200, { success: true, commits });
    });
}

async function handleGitBranches(res: http.ServerResponse) {
    execFile('git', ['branch', '-a'], { cwd: getWorkspaceRoot() }, (error: any, stdout: string) => {
        const branches = stdout.trim().split('\n').map(b => ({
            name: b.replace(/^\*?\s*/, '').trim(),
            current: b.startsWith('*')
        }));
        sendJson(res, 200, { success: true, branches });
    });
}

// ==================== DIAGNOSTICS HANDLERS ====================

async function handleFileDiagnostics(request: FileRequest, res: http.ServerResponse) {
    const fullPath = resolvePath(request.path);
    const uri = vscode.Uri.file(fullPath);
    const diagnostics = vscode.languages.getDiagnostics(uri);
    
    const results = diagnostics.map(d => ({
        line: d.range.start.line + 1,
        character: d.range.start.character + 1,
        message: d.message,
        severity: ['Error', 'Warning', 'Info', 'Hint'][d.severity],
        source: d.source
    }));
    
    sendJson(res, 200, { success: true, diagnostics: results });
}

// ==================== NETWORK HANDLERS ====================

async function handleFetch(request: FetchRequest, res: http.ServerResponse) {
    const parsedUrl = url.parse(request.url);
    const protocol = parsedUrl.protocol === 'https:' ? https : http;
    
    const options = {
        hostname: parsedUrl.hostname,
        port: parsedUrl.port,
        path: parsedUrl.path,
        method: request.method || 'GET',
        headers: request.headers || {}
    };
    
    const fetchReq = protocol.request(options, (fetchRes) => {
        let data = '';
        fetchRes.on('data', chunk => { data += chunk; });
        fetchRes.on('end', () => {
            sendJson(res, 200, {
                success: true,
                status: fetchRes.statusCode,
                headers: fetchRes.headers,
                body: data.substring(0, 500000)
            });
        });
    });
    
    fetchReq.on('error', (error: any) => {
        sendJson(res, 400, { success: false, error: error.message });
    });
    
    if (request.body) {
        fetchReq.write(request.body);
    }
    
    fetchReq.end();
}

// ==================== EDITOR HANDLERS ====================

async function handleOpenFile(data: any, res: http.ServerResponse) {
    const fullPath = resolvePath(data.path);
    const uri = vscode.Uri.file(fullPath);
    
    const doc = await vscode.workspace.openTextDocument(uri);
    const editor = await vscode.window.showTextDocument(doc);
    
    if (data.line) {
        const position = new vscode.Position(data.line - 1, data.character || 0);
        editor.selection = new vscode.Selection(position, position);
        editor.revealRange(new vscode.Range(position, position));
    }
    
    sendJson(res, 200, { success: true, path: fullPath });
}

async function handleInsertText(data: any, res: http.ServerResponse) {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        sendJson(res, 400, { success: false, error: 'No active editor' });
        return;
    }
    
    await editor.edit(editBuilder => {
        if (data.position === 'cursor') {
            editBuilder.insert(editor.selection.active, data.text);
        } else if (data.line !== undefined) {
            const pos = new vscode.Position(data.line - 1, data.character || 0);
            editBuilder.insert(pos, data.text);
        } else if (data.replace) {
            editBuilder.replace(editor.selection, data.text);
        }
    });
    
    sendJson(res, 200, { success: true });
}

async function handleGetSelection(res: http.ServerResponse) {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        sendJson(res, 200, { success: true, selection: null });
        return;
    }
    
    sendJson(res, 200, {
        success: true,
        file: vscode.workspace.asRelativePath(editor.document.uri),
        selection: {
            text: editor.document.getText(editor.selection),
            start: { line: editor.selection.start.line + 1, character: editor.selection.start.character },
            end: { line: editor.selection.end.line + 1, character: editor.selection.end.character }
        }
    });
}

// ==================== STREAMING CHAT HANDLER ====================

async function handleChatStream(request: ChatRequest, res: http.ServerResponse) {
    try {
        const models = await vscode.lm.selectChatModels({});
        if (models.length === 0) {
            sendJson(res, 200, { success: false, error: 'No language models available' });
            return;
        }

        let selectedModel = models[0];
        if (request.model) {
            const match = models.find(m =>
                m.id.toLowerCase().includes(request.model!.toLowerCase()) ||
                m.name.toLowerCase().includes(request.model!.toLowerCase()) ||
                m.family.toLowerCase().includes(request.model!.toLowerCase())
            );
            if (match) { selectedModel = match; }
        }

        const messages: vscode.LanguageModelChatMessage[] = [];
        if (request.systemPrompt) {
            messages.push(vscode.LanguageModelChatMessage.User(
                `[System Instructions]\n${request.systemPrompt}\n[End System Instructions]\n\n`
            ));
        }
        for (const msg of request.messages) {
            if (msg.role === 'user') {
                messages.push(vscode.LanguageModelChatMessage.User(msg.content));
            } else {
                messages.push(vscode.LanguageModelChatMessage.Assistant(msg.content));
            }
        }

        const modelOptions: Record<string, any> = {};
        if (request.temperature !== undefined) { modelOptions.temperature = request.temperature; }
        if (request.topP !== undefined) { modelOptions.top_p = request.topP; }
        if (request.maxTokens !== undefined) { modelOptions.max_tokens = request.maxTokens; }

        const requestOptions: vscode.LanguageModelChatRequestOptions = {
            ...(Object.keys(modelOptions).length > 0 ? { modelOptions } : {})
        };

        // SSE headers
        res.writeHead(200, {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Access-Control-Allow-Origin': '*'
        });

        const cts = new vscode.CancellationTokenSource();
        const requestId = `chat-${Date.now()}-${Math.random().toString(36).substring(7)}`;
        activeCancellations.set(requestId, cts);
        res.on('close', () => {
            cts.cancel();
            activeCancellations.delete(requestId);
        });

        // Send the request ID so client can cancel
        res.write(`data: ${JSON.stringify({ requestId })}\n\n`);

        try {
            const response = await selectedModel.sendRequest(messages, requestOptions, cts.token);
            for await (const chunk of response.text) {
                res.write(`data: ${JSON.stringify({ chunk })}\n\n`);
            }
            res.write(`data: ${JSON.stringify({ done: true, model: selectedModel.id })}\n\n`);
        } finally {
            cts.dispose();
            activeCancellations.delete(requestId);
        }
        res.end();
    } catch (error: any) {
        if (!res.headersSent) {
            sendJson(res, 500, { success: false, error: error.message });
        } else {
            res.write(`data: ${JSON.stringify({ error: error.message })}\n\n`);
            res.end();
        }
    }
}

// ==================== CODE INTELLIGENCE HANDLERS ====================

async function handleHover(request: UsagesRequest, res: http.ServerResponse) {
    const uri = vscode.Uri.file(resolvePath(request.path));
    const position = new vscode.Position(request.line - 1, request.character);

    const hovers = await vscode.commands.executeCommand<vscode.Hover[]>(
        'vscode.executeHoverProvider', uri, position
    );

    const results = (hovers || []).map(h => ({
        contents: h.contents.map(c => {
            if (typeof c === 'string') { return c; }
            if (c instanceof vscode.MarkdownString) { return c.value; }
            return (c as any).value || String(c);
        }),
        range: h.range ? {
            start: { line: h.range.start.line + 1, character: h.range.start.character },
            end: { line: h.range.end.line + 1, character: h.range.end.character }
        } : null
    }));

    sendJson(res, 200, { success: true, hovers: results });
}

async function handleCodeActions(data: any, res: http.ServerResponse) {
    const uri = vscode.Uri.file(resolvePath(data.path));
    const startPos = new vscode.Position((data.startLine || data.line || 1) - 1, data.startCharacter || 0);
    const endPos = new vscode.Position((data.endLine || data.startLine || data.line || 1) - 1, data.endCharacter || 999);
    const range = new vscode.Range(startPos, endPos);

    const actions = await vscode.commands.executeCommand<vscode.CodeAction[]>(
        'vscode.executeCodeActionProvider', uri, range
    );

    const results = (actions || []).map(a => ({
        title: a.title,
        kind: a.kind?.value,
        isPreferred: a.isPreferred,
        disabled: a.disabled?.reason
    }));

    sendJson(res, 200, { success: true, codeActions: results });
}

async function handleDocumentSymbols(request: FileRequest, res: http.ServerResponse) {
    const uri = vscode.Uri.file(resolvePath(request.path));

    const symbols = await vscode.commands.executeCommand<vscode.DocumentSymbol[]>(
        'vscode.executeDocumentSymbolProvider', uri
    );

    function flattenSymbols(syms: vscode.DocumentSymbol[], parentName?: string): any[] {
        const flat: any[] = [];
        for (const s of syms) {
            const qualifiedName = parentName ? `${parentName}.${s.name}` : s.name;
            flat.push({
                name: s.name,
                qualifiedName,
                kind: vscode.SymbolKind[s.kind],
                detail: s.detail,
                range: {
                    start: { line: s.range.start.line + 1, character: s.range.start.character },
                    end: { line: s.range.end.line + 1, character: s.range.end.character }
                }
            });
            if (s.children && s.children.length > 0) {
                flat.push(...flattenSymbols(s.children, qualifiedName));
            }
        }
        return flat;
    }

    sendJson(res, 200, { success: true, symbols: flattenSymbols(symbols || []) });
}

async function handleRenameSymbol(data: any, res: http.ServerResponse) {
    const uri = vscode.Uri.file(resolvePath(data.path));
    const position = new vscode.Position(data.line - 1, data.character);

    const edit = await vscode.commands.executeCommand<vscode.WorkspaceEdit>(
        'vscode.executeDocumentRenameProvider', uri, position, data.newName
    );

    if (!edit) {
        sendJson(res, 400, { success: false, error: 'Rename not available at this position' });
        return;
    }

    const applied = await vscode.workspace.applyEdit(edit);
    const entries = edit.entries();
    const files = entries.map(([fileUri]) => vscode.workspace.asRelativePath(fileUri));

    sendJson(res, 200, { success: applied, filesChanged: files });
}

async function handleCallHierarchy(data: any, res: http.ServerResponse) {
    const uri = vscode.Uri.file(resolvePath(data.path));
    const position = new vscode.Position(data.line - 1, data.character);

    const items = await vscode.commands.executeCommand<vscode.CallHierarchyItem[]>(
        'vscode.prepareCallHierarchy', uri, position
    );

    if (!items || items.length === 0) {
        sendJson(res, 200, { success: true, item: null, incomingCalls: [], outgoingCalls: [] });
        return;
    }

    const item = items[0];
    const direction = data.direction || 'both';

    let incomingCalls: any[] = [];
    let outgoingCalls: any[] = [];

    if (direction === 'incoming' || direction === 'both') {
        const incoming = await vscode.commands.executeCommand<vscode.CallHierarchyIncomingCall[]>(
            'vscode.provideIncomingCalls', item
        );
        incomingCalls = (incoming || []).map(c => ({
            name: c.from.name,
            kind: vscode.SymbolKind[c.from.kind],
            file: vscode.workspace.asRelativePath(c.from.uri),
            line: c.from.range.start.line + 1
        }));
    }

    if (direction === 'outgoing' || direction === 'both') {
        const outgoing = await vscode.commands.executeCommand<vscode.CallHierarchyOutgoingCall[]>(
            'vscode.provideOutgoingCalls', item
        );
        outgoingCalls = (outgoing || []).map(c => ({
            name: c.to.name,
            kind: vscode.SymbolKind[c.to.kind],
            file: vscode.workspace.asRelativePath(c.to.uri),
            line: c.to.range.start.line + 1
        }));
    }

    sendJson(res, 200, {
        success: true,
        item: { name: item.name, kind: vscode.SymbolKind[item.kind], file: vscode.workspace.asRelativePath(item.uri), line: item.range.start.line + 1 },
        incomingCalls,
        outgoingCalls
    });
}

// ==================== TERMINAL HANDLERS ====================

async function handleTerminalCreate(data: any, res: http.ServerResponse) {
    const terminal = vscode.window.createTerminal({
        name: data.name || 'Bridge Terminal',
        cwd: data.cwd ? resolvePath(data.cwd) : undefined,
        shellPath: data.shellPath,
        shellArgs: data.shellArgs
    });

    if (data.show !== false) {
        terminal.show(data.preserveFocus);
    }

    sendJson(res, 200, { success: true, name: terminal.name });
}

async function handleTerminalSend(data: any, res: http.ServerResponse) {
    const targetName = data.name;
    const terminal = vscode.window.terminals.find(t => t.name === targetName);

    if (!terminal) {
        sendJson(res, 404, { success: false, error: `Terminal '${targetName}' not found` });
        return;
    }

    terminal.sendText(data.text, data.addNewline !== false);
    sendJson(res, 200, { success: true });
}

async function handleTerminalDispose(data: any, res: http.ServerResponse) {
    const targetName = data.name;
    const terminal = vscode.window.terminals.find(t => t.name === targetName);

    if (!terminal) {
        sendJson(res, 404, { success: false, error: `Terminal '${targetName}' not found` });
        return;
    }

    terminal.dispose();
    terminalOutputBuffers.delete(targetName);
    sendJson(res, 200, { success: true });
}

async function handleTerminalOutput(data: any, res: http.ServerResponse) {
    const targetName = data.name;
    const buf = terminalOutputBuffers.get(targetName);

    if (!buf) {
        sendJson(res, 200, { success: true, output: '', lines: 0, note: 'No output captured or terminal not found' });
        return;
    }

    const lastN = data.lastLines || buf.length;
    const output = buf.slice(-lastN).join('');
    const clear = data.clear === true;
    if (clear) {
        buf.length = 0;
    }

    sendJson(res, 200, { success: true, output: output.substring(0, 200000), lines: buf.length });
}

// ==================== CANCELLATION + TOKEN COUNTING ====================

async function handleCancel(data: any, res: http.ServerResponse) {
    const id = data.id;
    if (!id) {
        // Cancel all active operations
        for (const [key, cts] of activeCancellations) {
            cts.cancel();
            cts.dispose();
        }
        const count = activeCancellations.size;
        activeCancellations.clear();
        sendJson(res, 200, { success: true, cancelled: count });
        return;
    }

    const cts = activeCancellations.get(id);
    if (cts) {
        cts.cancel();
        cts.dispose();
        activeCancellations.delete(id);
        sendJson(res, 200, { success: true, cancelled: 1 });
    } else {
        sendJson(res, 404, { success: false, error: `No active operation with id '${id}'` });
    }
}

async function handleTokenCount(data: any, res: http.ServerResponse) {
    // Estimate token count using the model's countTokens API
    const text = data.text || '';
    try {
        const models = await vscode.lm.selectChatModels({});
        if (models.length === 0) {
            // Rough estimate: ~4 chars per token
            sendJson(res, 200, { success: true, tokens: Math.ceil(text.length / 4), estimated: true });
            return;
        }

        let model = models[0];
        if (data.model) {
            const match = models.find(m =>
                m.id.toLowerCase().includes(data.model.toLowerCase()) ||
                m.family.toLowerCase().includes(data.model.toLowerCase())
            );
            if (match) { model = match; }
        }

        const message = vscode.LanguageModelChatMessage.User(text);
        const tokenCount = await model.countTokens(message);
        sendJson(res, 200, {
            success: true,
            tokens: tokenCount,
            maxInputTokens: model.maxInputTokens,
            model: model.id,
            estimated: false
        });
    } catch {
        sendJson(res, 200, { success: true, tokens: Math.ceil(text.length / 4), estimated: true });
    }
}

// ==================== GIT EXTENDED HANDLERS ====================

async function handleGitCommit(data: any, res: http.ServerResponse) {
    if (!data.message) {
        sendJson(res, 400, { success: false, error: 'Commit message required' });
        return;
    }

    const args = ['commit'];
    if (data.all) { args.push('-a'); }
    if (data.amend) { args.push('--amend'); }
    args.push('-m', data.message);

    execFile('git', args, { cwd: getWorkspaceRoot() }, (error: any, stdout: string, stderr: string) => {
        sendJson(res, 200, {
            success: !error,
            output: stdout || stderr,
            error: error ? error.message : undefined
        });
    });
}

async function handleGitStash(data: any, res: http.ServerResponse) {
    const action = data.action || 'push';
    const args = ['stash', action];
    if (action === 'push' && data.message) {
        args.push('-m', data.message);
    }
    if (action === 'push' && data.includeUntracked) {
        args.push('--include-untracked');
    }

    execFile('git', args, { cwd: getWorkspaceRoot() }, (error: any, stdout: string, stderr: string) => {
        sendJson(res, 200, {
            success: !error,
            output: stdout || stderr,
            error: error ? error.message : undefined
        });
    });
}

async function handleGitCheckout(data: any, res: http.ServerResponse) {
    if (!data.branch) {
        sendJson(res, 400, { success: false, error: 'Branch name required' });
        return;
    }

    const args = ['checkout'];
    if (data.create) { args.push('-b'); }
    args.push(data.branch);

    execFile('git', args, { cwd: getWorkspaceRoot() }, (error: any, stdout: string, stderr: string) => {
        sendJson(res, 200, {
            success: !error,
            output: stdout || stderr,
            error: error ? error.message : undefined
        });
    });
}

// ==================== WORKSPACE INDEX + SEMANTIC SEARCH ====================

const LANG_EXTENSIONS: Record<string, string> = {
    '.py': 'python', '.pyw': 'python',
    '.ts': 'typescript', '.tsx': 'typescriptreact',
    '.js': 'javascript', '.jsx': 'javascriptreact', '.mjs': 'javascript',
    '.java': 'java', '.c': 'c', '.cpp': 'cpp', '.cc': 'cpp', '.h': 'c', '.hpp': 'cpp',
    '.cs': 'csharp', '.go': 'go', '.rs': 'rust', '.rb': 'ruby', '.php': 'php',
    '.swift': 'swift', '.kt': 'kotlin', '.kts': 'kotlin',
    '.md': 'markdown', '.json': 'json', '.yaml': 'yaml', '.yml': 'yaml',
    '.html': 'html', '.htm': 'html', '.css': 'css', '.scss': 'scss',
    '.sql': 'sql', '.sh': 'shellscript', '.bat': 'bat', '.ps1': 'powershell',
    '.scala': 'scala', '.lua': 'lua', '.dart': 'dart', '.d2': 'd2',
};

interface IndexedFile {
    path: string;
    absPath: string;
    language: string;
    size: number;
    modified: number;
    termFreqs: Record<string, number>;
    termCount: number;
    summary: string;
    imports: string[];
    symbolNames: string[];
}

const wsIndex = {
    files: new Map<string, IndexedFile>(),
    idf: new Map<string, number>(),
    status: 'idle' as 'idle' | 'building' | 'ready',
    buildTime: 0,
    watcher: undefined as vscode.FileSystemWatcher | undefined,
};

// --- Tokenization ---
function tokenizeText(text: string): string[] {
    return text.toLowerCase()
        .split(/[^a-z0-9_]+/)
        .filter(t => t.length >= 2 && t.length <= 50);
}

function computeTermFrequencies(tokens: string[]): Record<string, number> {
    const freq: Record<string, number> = {};
    for (const t of tokens) { freq[t] = (freq[t] || 0) + 1; }
    return freq;
}

// --- Import extraction (per-language regex) ---
function extractImports(content: string, language: string): string[] {
    const imports: string[] = [];
    let m: RegExpExecArray | null;

    if (language === 'python') {
        const fromRe = /^\s*from\s+(\S+)\s+import/gm;
        const importRe = /^\s*import\s+(\S+)/gm;
        while ((m = fromRe.exec(content)) !== null) { imports.push(m[1]); }
        while ((m = importRe.exec(content)) !== null) { imports.push(m[1].split(',')[0].trim()); }
    } else if (['typescript', 'typescriptreact', 'javascript', 'javascriptreact'].includes(language)) {
        const fromRe = /from\s+['"]([^'"]+)['"]/gm;
        const reqRe = /require\s*\(\s*['"]([^'"]+)['"]\s*\)/gm;
        while ((m = fromRe.exec(content)) !== null) { imports.push(m[1]); }
        while ((m = reqRe.exec(content)) !== null) { imports.push(m[1]); }
    } else if (language === 'java') {
        const re = /^\s*import\s+([\w.]+)/gm;
        while ((m = re.exec(content)) !== null) { imports.push(m[1]); }
    } else if (['c', 'cpp'].includes(language)) {
        const re = /^\s*#include\s+["<]([^">]+)[">]/gm;
        while ((m = re.exec(content)) !== null) { imports.push(m[1]); }
    } else if (language === 'csharp') {
        const re = /^\s*using\s+([\w.]+)\s*;/gm;
        while ((m = re.exec(content)) !== null) { imports.push(m[1]); }
    } else if (language === 'go') {
        const block = content.match(/import\s*\(([\s\S]*?)\)/);
        if (block) {
            const lineRe = /^\s*"([^"]+)"/gm;
            while ((m = lineRe.exec(block[1])) !== null) { imports.push(m[1]); }
        }
        const single = /^\s*import\s+"([^"]+)"/gm;
        while ((m = single.exec(content)) !== null) { imports.push(m[1]); }
    } else if (language === 'rust') {
        const re = /^\s*use\s+([\w:]+)/gm;
        while ((m = re.exec(content)) !== null) { imports.push(m[1]); }
    }

    return [...new Set(imports)];
}

// --- Lightweight symbol name extraction (regex, no LSP) ---
function extractSymbolNames(content: string, language: string): string[] {
    const names: string[] = [];
    let m: RegExpExecArray | null;

    if (language === 'python') {
        const re = /^(?:class|def|async\s+def)\s+(\w+)/gm;
        while ((m = re.exec(content)) !== null) { names.push(m[1]); }
    } else if (['typescript', 'typescriptreact', 'javascript', 'javascriptreact'].includes(language)) {
        const re = /(?:export\s+)?(?:class|function|interface|type|enum|const|let|var)\s+(\w+)/gm;
        while ((m = re.exec(content)) !== null) { names.push(m[1]); }
    } else if (['java', 'csharp'].includes(language)) {
        const re = /(?:public|private|protected|internal|static)?\s*(?:class|interface|enum|record|struct)\s+(\w+)/gm;
        while ((m = re.exec(content)) !== null) { names.push(m[1]); }
    } else if (language === 'go') {
        const fnRe = /^func\s+(?:\([^)]*\)\s+)?(\w+)/gm;
        const typeRe = /^type\s+(\w+)/gm;
        while ((m = fnRe.exec(content)) !== null) { names.push(m[1]); }
        while ((m = typeRe.exec(content)) !== null) { names.push(m[1]); }
    } else if (language === 'rust') {
        const re = /(?:pub\s+)?(?:fn|struct|enum|trait|type|const)\s+(\w+)/gm;
        while ((m = re.exec(content)) !== null) { names.push(m[1]); }
    }

    return [...new Set(names)];
}

// --- Index a single file ---
async function indexSingleFile(uri: vscode.Uri): Promise<void> {
    const absPath = uri.fsPath;
    const relPath = vscode.workspace.asRelativePath(uri);
    const ext = path.extname(absPath).toLowerCase();
    const language = LANG_EXTENSIONS[ext];
    if (!language) { return; }

    let stat: fs.Stats;
    try { stat = fs.statSync(absPath); } catch { return; }
    if (stat.size > 500 * 1024 || stat.size === 0) { return; }

    let content: string;
    try { content = fs.readFileSync(absPath, 'utf-8'); } catch { return; }

    const tokens = tokenizeText(content);
    const termFreqs = computeTermFrequencies(tokens);
    const summary = content.split('\n').filter(l => l.trim()).slice(0, 10).join('\n');
    const imports = extractImports(content, language);
    const symbolNames = extractSymbolNames(content, language);

    wsIndex.files.set(relPath, {
        path: relPath, absPath, language,
        size: stat.size, modified: stat.mtimeMs,
        termFreqs, termCount: tokens.length,
        summary, imports, symbolNames,
    });
}

// --- Compute IDF across all indexed docs ---
function computeIDF() {
    wsIndex.idf.clear();
    const N = wsIndex.files.size;
    if (N === 0) { return; }

    const df = new Map<string, number>();
    for (const file of wsIndex.files.values()) {
        for (const term of Object.keys(file.termFreqs)) {
            df.set(term, (df.get(term) || 0) + 1);
        }
    }
    for (const [term, count] of df) {
        wsIndex.idf.set(term, Math.log(N / (1 + count)));
    }
}

// --- Full index build ---
async function buildWorkspaceIndex(): Promise<void> {
    if (wsIndex.status === 'building') { return; }
    wsIndex.status = 'building';
    const startTime = Date.now();
    console.log('Copilot Bridge: Building workspace index...');

    try {
        wsIndex.files.clear();
        const extensions = Object.keys(LANG_EXTENSIONS);
        const pattern = `**/*{${extensions.join(',')}}`;
        const excludes = '{**/node_modules/**,**/.git/**,**/out/**,**/dist/**,**/build/**,**/__pycache__/**,**/venv/**,**/.venv/**}';
        const uris = await vscode.workspace.findFiles(pattern, excludes, 5000);

        for (const uri of uris) {
            await indexSingleFile(uri);
        }
        computeIDF();
        wsIndex.buildTime = Date.now() - startTime;
        wsIndex.status = 'ready';
        console.log(`Copilot Bridge: Index built - ${wsIndex.files.size} files, ${wsIndex.idf.size} terms in ${wsIndex.buildTime}ms`);
    } catch (err) {
        console.error('Copilot Bridge: Index build failed:', err);
        wsIndex.status = 'idle';
    }
}

// --- FileSystemWatcher for incremental updates ---
function setupFileWatcher(context: vscode.ExtensionContext) {
    const watchPattern = '**/*.{py,pyw,ts,tsx,js,jsx,mjs,java,c,cpp,cc,h,hpp,cs,go,rs,rb,php,swift,kt,kts,md,json,yaml,yml,html,css,scss,sql,sh,bat,ps1,scala,lua,dart,d2}';
    wsIndex.watcher = vscode.workspace.createFileSystemWatcher(watchPattern);

    wsIndex.watcher.onDidChange(async (uri) => {
        await indexSingleFile(uri);
        computeIDF();
    });
    wsIndex.watcher.onDidCreate(async (uri) => {
        await indexSingleFile(uri);
        computeIDF();
    });
    wsIndex.watcher.onDidDelete((uri) => {
        wsIndex.files.delete(vscode.workspace.asRelativePath(uri));
        computeIDF();
    });

    context.subscriptions.push(wsIndex.watcher);
}

// --- TF-IDF Search ---
function tfidfSearch(query: string, maxResults: number = 20): Array<{path: string; score: number; summary: string; language: string}> {
    const queryTokens = tokenizeText(query);
    if (queryTokens.length === 0) { return []; }

    // Expand camelCase and snake_case
    const expanded = new Set(queryTokens);
    for (const token of queryTokens) {
        for (const p of token.replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase().split(/\s+/)) {
            if (p.length >= 2) { expanded.add(p); }
        }
        for (const p of token.split('_')) {
            if (p.length >= 2) { expanded.add(p); }
        }
    }

    const results: Array<{path: string; score: number; summary: string; language: string}> = [];

    for (const [filePath, file] of wsIndex.files) {
        let score = 0;

        for (const term of expanded) {
            const tf = (file.termFreqs[term] || 0) / Math.max(file.termCount, 1);
            const idf = wsIndex.idf.get(term) || 0;
            score += tf * idf;
        }

        // Boost for symbol name match
        const symLowers = file.symbolNames.map(s => s.toLowerCase());
        for (const term of expanded) {
            if (symLowers.some(s => s.includes(term))) { score *= 1.5; break; }
        }

        // Boost for filename match
        const fileNameLower = path.basename(filePath).toLowerCase();
        for (const term of expanded) {
            if (fileNameLower.includes(term)) { score *= 2.0; break; }
        }

        if (score > 0) {
            results.push({ path: filePath, score, summary: file.summary, language: file.language });
        }
    }

    results.sort((a, b) => b.score - a.score);
    return results.slice(0, maxResults);
}

// --- LLM query expansion ---
async function llmQueryExpansion(query: string): Promise<string[]> {
    try {
        const models = await vscode.lm.selectChatModels({});
        if (models.length === 0) { return []; }

        const messages = [
            vscode.LanguageModelChatMessage.User(
                `Generate 5-8 specific text search patterns to find code related to this query. Return ONLY a JSON array of strings, no explanation.\n\nQuery: "${query}"\n\nExample output: ["pattern1", "pattern2"]`
            )
        ];

        const cts = new vscode.CancellationTokenSource();
        try {
            const response = await models[0].sendRequest(messages, {}, cts.token);
            let text = '';
            for await (const chunk of response.text) { text += chunk; }

            const match = text.match(/\[[\s\S]*?\]/);
            if (match) {
                const patterns = JSON.parse(match[0]);
                if (Array.isArray(patterns)) {
                    return patterns.filter((p: any) => typeof p === 'string').slice(0, 8);
                }
            }
        } finally { cts.dispose(); }
    } catch {}
    return [];
}

// --- LLM relevance ranking ---
async function llmRankResults(query: string, candidates: Array<{path: string; summary: string}>): Promise<Array<{path: string; score: number; reason: string}>> {
    if (candidates.length === 0) { return []; }

    try {
        const models = await vscode.lm.selectChatModels({});
        if (models.length === 0) {
            return candidates.map(c => ({ path: c.path, score: 5, reason: 'no model' }));
        }

        const fileList = candidates.slice(0, 20).map((c, i) =>
            `${i + 1}. ${c.path}\n   ${c.summary.substring(0, 150).replace(/\n/g, ' ')}`
        ).join('\n');

        const messages = [
            vscode.LanguageModelChatMessage.User(
                `Rate each file's relevance to this query on 0-10 scale. Return ONLY a JSON array of objects with "index" (1-based), "score" (0-10), "reason" (brief). No other text.\n\nQuery: "${query}"\n\nFiles:\n${fileList}`
            )
        ];

        const cts = new vscode.CancellationTokenSource();
        try {
            const response = await models[0].sendRequest(messages, {}, cts.token);
            let text = '';
            for await (const chunk of response.text) { text += chunk; }

            const match = text.match(/\[[\s\S]*?\]/);
            if (match) {
                const scores = JSON.parse(match[0]);
                if (Array.isArray(scores)) {
                    return scores
                        .filter((s: any) => typeof s.index === 'number' && typeof s.score === 'number')
                        .map((s: any) => ({
                            path: candidates[s.index - 1]?.path || '',
                            score: s.score,
                            reason: s.reason || ''
                        }))
                        .filter((s: any) => s.path);
                }
            }
        } finally { cts.dispose(); }
    } catch {}

    return candidates.map(c => ({ path: c.path, score: 5, reason: 'LLM ranking failed' }));
}

// --- Combined semantic search (TF-IDF + LLM expansion + LLM ranking) ---
async function performSemanticSearch(query: string, maxResults: number = 20, useLLM: boolean = true): Promise<any> {
    if (wsIndex.status !== 'ready') {
        return { success: false, error: 'Index not ready. Call /workspace/reindex first.' };
    }

    // Step 1: TF-IDF search (fast, in-memory)
    const tfidfResults = tfidfSearch(query, maxResults * 2);

    // Step 2: LLM query expansion → more TF-IDF searches
    let expandedResults: typeof tfidfResults = [];
    let expansionTerms: string[] = [];

    if (useLLM) {
        expansionTerms = await llmQueryExpansion(query);
        for (const term of expansionTerms) {
            expandedResults.push(...tfidfSearch(term, 10));
        }
    }

    // Step 3: Merge (dedupe, blend scores)
    const scoreMap = new Map<string, {score: number; summary: string; language: string}>();

    for (const r of tfidfResults) {
        scoreMap.set(r.path, { score: r.score, summary: r.summary, language: r.language });
    }
    for (const r of expandedResults) {
        const existing = scoreMap.get(r.path);
        if (existing) {
            existing.score += r.score * 0.5;
        } else {
            scoreMap.set(r.path, { score: r.score * 0.5, summary: r.summary, language: r.language });
        }
    }

    let candidates = Array.from(scoreMap.entries())
        .map(([p, d]) => ({ path: p, score: d.score, summary: d.summary, language: d.language, reason: '' }))
        .sort((a, b) => b.score - a.score)
        .slice(0, maxResults * 2);

    // Step 4: LLM relevance re-ranking
    if (useLLM && candidates.length > 0) {
        const llmScores = await llmRankResults(query, candidates.slice(0, 20));
        const llmMap = new Map(llmScores.map(s => [s.path, s]));

        for (const c of candidates) {
            const llm = llmMap.get(c.path);
            if (llm) {
                c.score = c.score * (1 + llm.score / 10);
                c.reason = llm.reason;
            }
        }
        candidates.sort((a, b) => b.score - a.score);
    }

    const results = candidates.slice(0, maxResults).map(c => ({
        path: c.path,
        score: Math.round(c.score * 1000) / 1000,
        language: c.language,
        summary: c.summary,
        reason: c.reason || undefined,
    }));

    return {
        success: true,
        results,
        meta: {
            indexedFiles: wsIndex.files.size,
            tfidfHits: tfidfResults.length,
            expansionTerms: expansionTerms.length > 0 ? expansionTerms : undefined,
            llmRanked: useLLM,
        }
    };
}

// --- Find related files (via imports, symbols, co-location) ---
function findRelatedFiles(filePath: string, maxResults: number = 10): any {
    const file = wsIndex.files.get(filePath);
    if (!file) {
        return { success: false, error: 'File not in index' };
    }

    const related = new Map<string, {score: number; reasons: string[]}>();

    function addRelation(otherPath: string, score: number, reason: string) {
        if (otherPath === filePath) { return; }
        const existing = related.get(otherPath) || { score: 0, reasons: [] };
        existing.score += score;
        existing.reasons.push(reason);
        related.set(otherPath, existing);
    }

    const baseName = path.basename(filePath, path.extname(filePath));

    // 1. Reverse dependencies: files that import this file
    for (const [otherPath, otherFile] of wsIndex.files) {
        for (const imp of otherFile.imports) {
            if (imp.includes(baseName)) {
                addRelation(otherPath, 5, `imports ${baseName}`);
                break;
            }
        }
    }

    // 2. Forward dependencies: files this file imports
    for (const imp of file.imports) {
        for (const [otherPath] of wsIndex.files) {
            const otherBase = path.basename(otherPath, path.extname(otherPath));
            if (imp.includes(otherBase)) {
                addRelation(otherPath, 4, `imported as ${imp}`);
                break;
            }
        }
    }

    // 3. Shared symbol names
    const mySymbols = new Set(file.symbolNames.map(s => s.toLowerCase()));
    for (const [otherPath, otherFile] of wsIndex.files) {
        let shared = 0;
        for (const sym of otherFile.symbolNames) {
            if (mySymbols.has(sym.toLowerCase())) { shared++; }
        }
        if (shared > 0) {
            addRelation(otherPath, shared * 2, `${shared} shared symbols`);
        }
    }

    // 4. Same directory
    const dir = path.dirname(filePath);
    for (const [otherPath] of wsIndex.files) {
        if (path.dirname(otherPath) === dir) {
            addRelation(otherPath, 1, 'same directory');
        }
    }

    const results = Array.from(related.entries())
        .map(([p, d]) => ({ path: p, score: d.score, reason: d.reasons.join('; ') }))
        .sort((a, b) => b.score - a.score)
        .slice(0, maxResults);

    return { success: true, file: filePath, related: results };
}

// --- Workspace index HTTP handlers ---
async function handleSemanticSearch(data: any, res: http.ServerResponse) {
    const result = await performSemanticSearch(
        data.query || '', data.maxResults || 20, data.useLLM !== false
    );
    sendJson(res, 200, result);
}

function handleWorkspaceIndexInfo(res: http.ServerResponse) {
    const langs = new Map<string, number>();
    let totalSymbols = 0;
    let totalImports = 0;

    for (const file of wsIndex.files.values()) {
        langs.set(file.language, (langs.get(file.language) || 0) + 1);
        totalSymbols += file.symbolNames.length;
        totalImports += file.imports.length;
    }

    sendJson(res, 200, {
        success: true,
        status: wsIndex.status,
        fileCount: wsIndex.files.size,
        symbolCount: totalSymbols,
        importEdges: totalImports,
        uniqueTerms: wsIndex.idf.size,
        buildTimeMs: wsIndex.buildTime,
        languages: Object.fromEntries(langs),
    });
}

function handleWorkspaceIndexFiles(res: http.ServerResponse) {
    const files = Array.from(wsIndex.files.values()).map(f => ({
        path: f.path, language: f.language, size: f.size,
        symbols: f.symbolNames.length, imports: f.imports.length,
    }));
    sendJson(res, 200, { success: true, files });
}

function handleRelatedFiles(data: any, res: http.ServerResponse) {
    sendJson(res, 200, findRelatedFiles(data.path, data.maxResults || 10));
}

function handleImportGraph(data: any, res: http.ServerResponse) {
    const filePath = data.path;

    if (filePath) {
        const file = wsIndex.files.get(filePath);
        if (!file) {
            sendJson(res, 404, { success: false, error: 'File not in index' });
            return;
        }
        // Reverse dependencies
        const importedBy: string[] = [];
        const baseName = path.basename(filePath, path.extname(filePath));
        for (const [otherPath, otherFile] of wsIndex.files) {
            if (otherPath === filePath) { continue; }
            for (const imp of otherFile.imports) {
                if (imp.includes(baseName)) { importedBy.push(otherPath); break; }
            }
        }
        sendJson(res, 200, {
            success: true, file: filePath,
            imports: file.imports, importedBy, symbols: file.symbolNames,
        });
    } else {
        const edges: Array<{from: string; to: string; module: string}> = [];
        for (const [fp, file] of wsIndex.files) {
            for (const imp of file.imports) {
                edges.push({ from: fp, to: imp, module: imp });
            }
        }
        sendJson(res, 200, { success: true, edges, fileCount: wsIndex.files.size });
    }
}

async function handleReindex(res: http.ServerResponse) {
    await buildWorkspaceIndex();
    handleWorkspaceIndexInfo(res);
}

// ==================== COPILOT INSTRUCTIONS ====================

async function handleCopilotInstructions(res: http.ServerResponse) {
    const root = getWorkspaceRoot();
    const instructionsPath = path.join(root, '.github', 'copilot-instructions.md');

    if (fs.existsSync(instructionsPath)) {
        const content = fs.readFileSync(instructionsPath, 'utf-8');
        sendJson(res, 200, { success: true, content, path: instructionsPath, exists: true });
    } else {
        sendJson(res, 200, { success: true, content: '', path: instructionsPath, exists: false });
    }
}

// ==================== IMAGE CHAT HANDLER ====================

async function handleChatWithImage(data: any, res: http.ServerResponse): Promise<void> {
    try {
        // LanguageModelDataPart requires VS Code 1.97+
        const DataPartClass = (vscode as any).LanguageModelDataPart;
        const TextPartClass = (vscode as any).LanguageModelTextPart;
        if (!DataPartClass || !TextPartClass) {
            sendJson(res, 400, { success: false, error: 'Image chat requires VS Code 1.97+. Update VS Code and retry.' });
            return;
        }

        const models = await vscode.lm.selectChatModels({});
        if (models.length === 0) {
            sendJson(res, 200, { success: false, error: 'No language models available' });
            return;
        }

        let selectedModel = models[0];
        if (data.model) {
            const match = models.find(m =>
                m.id.toLowerCase().includes(data.model.toLowerCase()) ||
                m.name.toLowerCase().includes(data.model.toLowerCase()) ||
                m.family.toLowerCase().includes(data.model.toLowerCase())
            );
            if (match) { selectedModel = match; }
        }

        const imagePath = resolvePath(data.imagePath);
        if (!fs.existsSync(imagePath)) {
            sendJson(res, 404, { success: false, error: 'Image file not found' });
            return;
        }

        const imageData = fs.readFileSync(imagePath);
        const ext = path.extname(imagePath).toLowerCase();
        const mimeTypes: Record<string, string> = {
            '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.gif': 'image/gif', '.webp': 'image/webp'
        };
        const mimeType = mimeTypes[ext] || 'image/png';

        const userContent: any[] = [];
        if (data.message) {
            userContent.push(new TextPartClass(data.message));
        }
        userContent.push(new DataPartClass(new Uint8Array(imageData), mimeType));

        const messages: vscode.LanguageModelChatMessage[] = [];
        if (data.systemPrompt) {
            messages.push(vscode.LanguageModelChatMessage.User(
                `[System Instructions]\n${data.systemPrompt}\n[End System Instructions]\n\n`
            ));
        }
        for (const msg of (data.messages || [])) {
            if (msg.role === 'user') {
                messages.push(vscode.LanguageModelChatMessage.User(msg.content));
            } else {
                messages.push(vscode.LanguageModelChatMessage.Assistant(msg.content));
            }
        }
        messages.push(vscode.LanguageModelChatMessage.User(userContent));

        const modelOptions: Record<string, any> = {};
        if (data.temperature !== undefined) { modelOptions.temperature = data.temperature; }
        if (data.maxTokens !== undefined) { modelOptions.max_tokens = data.maxTokens; }

        const cts = new vscode.CancellationTokenSource();
        try {
            const response = await selectedModel.sendRequest(
                messages,
                Object.keys(modelOptions).length > 0 ? { modelOptions } : {},
                cts.token
            );
            let fullResponse = '';
            for await (const chunk of response.text) { fullResponse += chunk; }
            sendJson(res, 200, { success: true, content: fullResponse, model: selectedModel.id });
        } finally {
            cts.dispose();
        }
    } catch (error: any) {
        sendJson(res, 500, { success: false, error: error.message });
    }
}

// ==================== GIT ADD / PUSH / PULL / MERGE ====================

async function handleGitAdd(data: any, res: http.ServerResponse) {
    const files = data.files;
    if (!files || (Array.isArray(files) && files.length === 0)) {
        sendJson(res, 400, { success: false, error: 'files required (string or string[])' });
        return;
    }

    let args: string[];
    if (files === '.' || files === '-A' || files === '--all') {
        args = ['add', '-A'];
    } else if (Array.isArray(files)) {
        args = ['add', '--', ...files];
    } else {
        args = ['add', '--', files];
    }

    execFile('git', args, { cwd: getWorkspaceRoot() }, (error: any, stdout: string, stderr: string) => {
        sendJson(res, 200, {
            success: !error,
            output: (stdout || stderr).trim(),
            error: error ? error.message : undefined
        });
    });
}

async function handleGitPush(data: any, res: http.ServerResponse) {
    const args = ['push'];
    const remote = data.remote || 'origin';
    args.push(remote);
    if (data.branch) { args.push(data.branch); }
    if (data.setUpstream) { args.push('--set-upstream'); }
    if (data.force) { args.push('--force-with-lease'); }
    if (data.tags) { args.push('--tags'); }

    execFile('git', args, { cwd: getWorkspaceRoot(), timeout: 60000 }, (error: any, stdout: string, stderr: string) => {
        sendJson(res, 200, {
            success: !error,
            output: (stdout || stderr).trim(),
            error: error ? error.message : undefined
        });
    });
}

async function handleGitPull(data: any, res: http.ServerResponse) {
    const args = ['pull'];
    if (data.rebase) { args.push('--rebase'); }
    if (data.remote) { args.push(data.remote); }
    if (data.branch) { args.push(data.branch); }

    execFile('git', args, { cwd: getWorkspaceRoot(), timeout: 60000 }, (error: any, stdout: string, stderr: string) => {
        sendJson(res, 200, {
            success: !error,
            output: (stdout || stderr).trim(),
            error: error ? error.message : undefined
        });
    });
}

async function handleGitMerge(data: any, res: http.ServerResponse) {
    if (!data.branch) {
        sendJson(res, 400, { success: false, error: 'branch required' });
        return;
    }

    const args = ['merge'];
    if (data.noFf) { args.push('--no-ff'); }
    if (data.squash) { args.push('--squash'); }
    if (data.message) { args.push('-m', data.message); }
    args.push(data.branch);

    execFile('git', args, { cwd: getWorkspaceRoot() }, (error: any, stdout: string, stderr: string) => {
        sendJson(res, 200, {
            success: !error,
            output: (stdout || stderr).trim(),
            error: error ? error.message : undefined
        });
    });
}

// ==================== SERVER CONTROL ====================

function stopServer() {
    if (server) {
        server.close();
        server = undefined;
        const port = activePort;
        activePort = undefined;
        statusBarItem.hide();
        vscode.window.showInformationMessage(`Copilot Bridge server stopped (was on port ${port})`);
    }
}

function showStatus() {
    if (server && activePort) {
        const config = vscode.workspace.getConfiguration('copilotBridge');
        const configuredPort = config.get<number>('port', 5150);
        const portInfo = activePort !== configuredPort
            ? `port ${activePort} (configured: ${configuredPort})`
            : `port ${activePort}`;
        
        const features = [
            'Chat (LLM)', 'Files (CRUD)', 'Search (Text/Symbol/Usages)',
            'Commands', 'Git', 'Diagnostics', 'Fetch', 'VS Code Commands',
            'Editor Control', 'Notifications'
        ];
        
        vscode.window.showInformationMessage(
            `Copilot Bridge v${extVersion} on ${portInfo}\nFeatures: ${features.join(', ')}`
        );
    } else {
        vscode.window.showWarningMessage('Copilot Bridge server is not running');
    }
}

export function deactivate() {
    stopServer();
}
