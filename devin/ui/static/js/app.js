/**
 * DEVIN AI IDE — Web UI Controller
 */

const App = {
    state: {
        running: false,
        currentRunId: null,
        eventSource: null,
        rigHealth: null
    },

    init() {
        this.bindEvents();
        this.checkRigHealth();
        setInterval(() => this.checkRigHealth(), 5000);
    },

    bindEvents() {
        document.getElementById('runBtn')?.addEventListener('click', () => this.startRun());
        document.getElementById('clearLogBtn')?.addEventListener('click', () => this.clearLog());
        document.getElementById('themeToggle')?.addEventListener('click', () => this.toggleTheme());
        document.getElementById('applyPatchBtn')?.addEventListener('click', () => this.applyPatch());
        document.getElementById('rejectPatchBtn')?.addEventListener('click', () => this.rejectPatch());
    },

    // Theme
    toggleTheme() {
        const html = document.documentElement;
        const current = html.getAttribute('data-theme');
        const next = current === 'dark' ? 'light' : 'dark';
        html.setAttribute('data-theme', next);
        localStorage.setItem('devin-theme', next);
    },

    loadTheme() {
        const saved = localStorage.getItem('devin-theme') || 'dark';
        document.documentElement.setAttribute('data-theme', saved);
    },

    // Rig Health
    async checkRigHealth() {
        try {
            const res = await fetch('/api/health');
            const data = await res.json();
            this.state.rigHealth = data;
            this.updateRigIndicator(data);
        } catch (e) {
            this.updateRigIndicator({ remote_coder: false, remote_reasoning: false });
        }
    },

    updateRigIndicator(data) {
        const indicator = document.getElementById('rigIndicator');
        const label = document.getElementById('rigLabel');
        if (!indicator || !label) return;

        const coder = data.remote_coder;
        const reasoning = data.remote_reasoning;

        indicator.className = 'indicator';
        if (coder && reasoning) {
            indicator.classList.add('online');
            label.textContent = 'Rig 32B Online';
        } else if (coder || reasoning) {
            indicator.classList.add('partial');
            label.textContent = 'Rig Partial';
        } else {
            indicator.classList.add('offline');
            label.textContent = 'Rig Offline';
        }
    },

    // Run Agent
    async startRun() {
        if (this.state.running) return;

        const path = document.getElementById('projectPath').value.trim();
        const task = document.getElementById('taskInput').value.trim();
        const entrypoint = document.getElementById('entrypoint').value.trim() || null;
        const maxAttempts = parseInt(document.getElementById('maxAttempts').value) || 3;

        if (!path) {
            alert('Inserisci un project path');
            return;
        }

        this.state.running = true;
        this.state.currentRunId = null;

        const btn = document.getElementById('runBtn');
        btn.classList.add('running');
        btn.innerHTML = '<span class="btn-icon">&#9203;</span> Running...';

        this.clearLog();
        this.appendLog('Starting agent...', 'info');

        try {
            const res = await fetch('/api/run', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    path,
                    task,
                    entrypoint,
                    max_attempts: maxAttempts,
                    max_seconds: 300
                })
            });
            const data = await res.json();

            if (data.error) {
                this.appendLog('Error: ' + data.error, 'error');
                this.resetRun();
                return;
            }

            this.state.currentRunId = data.run_id;
            document.getElementById('runIdDisplay').textContent = data.run_id;
            this.appendLog('Run ID: ' + data.run_id, 'info');
            this.startLogStream(data.run_id);

        } catch (e) {
            this.appendLog('Network error: ' + e.message, 'error');
            this.resetRun();
        }
    },

    // SSE Log Streaming
    startLogStream(runId) {
        if (this.state.eventSource) {
            this.state.eventSource.close();
        }

        const es = new EventSource('/stream/' + runId);
        this.state.eventSource = es;

        es.onmessage = (e) => {
            try {
                const payload = JSON.parse(e.data);
                if (payload.type === 'log') {
                    this.appendLogLine(payload.line);
                } else if (payload.type === 'error') {
                    this.appendLog(payload.msg, 'error');
                    this.resetRun();
                }
            } catch (err) {
                this.appendLogLine(e.data);
            }
        };

        es.onerror = () => {
            setTimeout(() => {
                this.checkRunStatus(runId);
            }, 2000);
        };
    },

    async checkRunStatus(runId) {
        try {
            const res = await fetch('/api/run/' + runId + '/log');
            const data = await res.json();
            if (data.content) {
                const lines = data.content.split('\n');
                const lastLines = lines.slice(-5).join(' ').toLowerCase();
                if (lastLines.includes('run completato') || lastLines.includes('status:')) {
                    this.appendLog('Run completed. Check History for details.', 'success');
                    this.resetRun();
                }
            }
        } catch (e) {
            console.error('Status check failed', e);
        }
    },

    // Log Helpers
    appendLogLine(line) {
        let type = 'info';
        const lower = line.toLowerCase();
        if (lower.includes('error') || lower.includes('fallita')) type = 'error';
        else if (lower.includes('success') || lower.includes('completato')) type = 'success';
        else if (lower.includes('warning') || lower.includes('timeout')) type = 'warning';
        else if (lower.includes('tentativo')) type = 'debug';
        this.appendLog(line, type);
    },

    appendLog(text, type) {
        const console = document.getElementById('logConsole');
        if (!console) return;

        const div = document.createElement('div');
        div.className = 'log-line log-' + type;
        div.textContent = text;
        console.appendChild(div);
        console.scrollTop = console.scrollHeight;
    },

    clearLog() {
        const console = document.getElementById('logConsole');
        if (console) {
            console.innerHTML = '<div class="log-line log-info">Ready. Click "Run Agent" to start.</div>';
        }
        document.getElementById('runIdDisplay').textContent = '—';
    },

    // Diff / Patch
    showDiff(patchText) {
        const container = document.getElementById('diffContainer');
        const actions = document.getElementById('diffActions');
        if (!container) return;

        if (!patchText || !patchText.trim()) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">&#9989;</div><p>No patch generated — code was already correct or no action needed.</p></div>';
            actions.style.display = 'none';
            return;
        }

        const lines = patchText.split('\n');
        let html = '<div class="diff-view">';
        for (const line of lines) {
            let cls = 'diff-line';
            let content = line;
            if (line.startsWith('diff --git')) { cls += ' diff-header'; }
            else if (line.startsWith('---')) { cls += ' diff-meta'; }
            else if (line.startsWith('+++')) { cls += ' diff-meta'; }
            else if (line.startsWith('@@')) { cls += ' diff-hunk'; }
            else if (line.startsWith('+')) { cls += ' diff-add'; content = line; }
            else if (line.startsWith('-')) { cls += ' diff-del'; content = line; }
            else if (line.startsWith(' ')) { cls += ' diff-ctx'; }
            html += '<div class="' + cls + '"><span class="diff-marker">' + content.slice(0,1) + '</span><span class="diff-text">' + this.escapeHtml(content.slice(1)) + '</span></div>';
        }
        html += '</div>';

        container.innerHTML = html;
        actions.style.display = 'flex';
    },

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    },

    applyPatch() {
        this.appendLog('Patch applied (placeholder — integrate with backend)', 'success');
    },

    rejectPatch() {
        document.getElementById('diffContainer').innerHTML = '<div class="empty-state"><div class="empty-icon">&#10060;</div><p>Patch rejected.</p></div>';
        document.getElementById('diffActions').style.display = 'none';
        this.appendLog('Patch rejected by user', 'warning');
    },

    // Reset
    resetRun() {
        this.state.running = false;
        this.state.currentRunId = null;
        if (this.state.eventSource) {
            this.state.eventSource.close();
            this.state.eventSource = null;
        }
        const btn = document.getElementById('runBtn');
        if (btn) {
            btn.classList.remove('running');
            btn.innerHTML = '<span class="btn-icon">&#9654;</span> Run Agent';
        }
    }
};

// Diff CSS (injected)
const diffStyles = document.createElement('style');
diffStyles.textContent = `
.diff-view {
    font-family: var(--font-mono);
    font-size: 12px;
    line-height: 1.6;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: auto;
    max-height: 100%;
}
.diff-line {
    padding: 1px 8px;
    white-space: pre-wrap;
    word-break: break-all;
    display: flex;
}
.diff-line:hover { background: var(--bg-tertiary); }
.diff-header { color: var(--accent); font-weight: 600; background: var(--bg-secondary); }
.diff-meta { color: var(--text-secondary); background: var(--bg-secondary); }
.diff-hunk { color: var(--info); background: var(--bg-tertiary); }
.diff-add { background: rgba(63, 185, 80, 0.1); color: var(--success); }
.diff-del { background: rgba(248, 81, 73, 0.1); color: var(--danger); }
.diff-ctx { color: var(--text-primary); }
.diff-marker {
    display: inline-block;
    width: 16px;
    flex-shrink: 0;
    text-align: center;
    margin-right: 4px;
    opacity: 0.7;
}
.diff-text { flex: 1; }
`;
document.head.appendChild(diffStyles);

// Bootstrap
document.addEventListener('DOMContentLoaded', () => {
    App.loadTheme();
    App.init();
});
