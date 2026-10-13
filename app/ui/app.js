let currentJobId = null;
let pollInterval = null;
let bulkPublishingJobId = null;
const ACTIVE_JOB_KEY = 'justclipper.activeJobId';

function rememberJobId(jobId) {
    currentJobId = jobId;
    try { sessionStorage.setItem(ACTIVE_JOB_KEY, jobId); } catch (_) { /* Private mode. */ }
}

function forgetJobId() {
    currentJobId = null;
    try { sessionStorage.removeItem(ACTIVE_JOB_KEY); } catch (_) { /* Private mode. */ }
}

function showAnalysisStatus(message, pending = false) {
    const status = document.getElementById('analysis-status');
    const progress = document.getElementById('analysis-progress-container');
    status.style.display = 'block';
    status.textContent = message;
    progress.style.display = pending ? 'block' : 'none';
}

function applyAnalysisSettings(settings) {
    if (!settings) return;
    const val = settings.count_mode === 'TARGET' ? String(settings.target_clip_count) : 'AUTO';
    window.globalClipCount = val;
    document.querySelectorAll(`input[type="radio"][value="${val}"]`).forEach(r => r.checked = true);
    const target = document.getElementById('setting-target-count');
    if (target) target.value = val;
    if (settings.clip_count) document.getElementById('setting-clip-count').value = settings.clip_count;
    if (settings.min_clip_seconds) document.getElementById('setting-min-sec').value = settings.min_clip_seconds;
    if (settings.max_clip_seconds) document.getElementById('setting-max-sec').value = settings.max_clip_seconds;
    if (settings.editorial_guidance) document.getElementById('setting-guidance').value = settings.editorial_guidance;
    if (settings.aspect_mode) document.getElementById('setting-aspect').value = settings.aspect_mode;
    if (settings.output_resolution) document.getElementById('setting-resolution').value = settings.output_resolution;
    if (settings.exact_clip_count === true) document.getElementById('setting-exact-count').checked = true;
}

async function reopenAnalysisSettings() {
    if (!currentJobId) return;
    try {
        const res = await fetch(`/api/status/${encodeURIComponent(currentJobId)}`);
        if (res.ok) applyAnalysisSettings((await res.json()).analysis_settings);
    } catch (_) { /* Keep the current form values. */ }
    document.getElementById('setting-exact-count').checked = true;
    const button = document.getElementById('btn-analyze');
    button.style.display = '';
    button.disabled = false;
    button.textContent = 'Analyze with Gemini';
    showStep('step-settings');
    showAnalysisStatus('Review the count and durations, then start analysis.');
}

async function restoreCurrentJob() {
    let jobId;
    try { jobId = sessionStorage.getItem(ACTIVE_JOB_KEY); } catch (_) { return; }
    if (!jobId || !/^[A-Za-z0-9_-]{1,80}$/.test(jobId)) return;
    try {
        const res = await fetch(`/api/status/${encodeURIComponent(jobId)}`);
        if (!res.ok) { forgetJobId(); return; }
        const job = await res.json();
        rememberJobId(jobId);
        applyAnalysisSettings(job.analysis_settings);
        if (job.status === 'COMPLETED') {
            displayResults(job);
        } else if (job.status === 'READY_TO_RENDER') {
            displayCandidates(job);
        } else {
            if (job.metadata) displayMetadata(job.metadata);
            showStep('step-settings');
            const analyzing = ['QUEUED_ANALYSIS', 'PREPARING_ANALYSIS_MEDIA',
                'GENERATING_PROXY', 'RUNNING_LOCAL_ANALYSIS', 'UPLOADING_TO_GEMINI',
                'WAITING_FOR_GEMINI', 'VALIDATING_CANDIDATES'].includes(job.status);
            if (analyzing) {
                const btn = document.getElementById('btn-analyze');
                btn.disabled = true;
                btn.textContent = 'Analyzing...';
                showAnalysisStatus(`Analysis in progress: ${job.status}`, true);
                pollJobStatus(job.auto_render ? 'COMPLETED' : 'READY_TO_RENDER', job.auto_render ? displayResults : displayCandidates);
            } else if (['QUEUED_RENDER', 'RENDERING'].includes(job.status)) {
                const btn = document.getElementById('btn-analyze');
                btn.disabled = true;
                btn.textContent = 'Rendering...';
                showAnalysisStatus(`Rendering in progress: ${job.status}`, true);
                pollJobStatus('COMPLETED', displayResults);
            } else if (job.status === 'FAILED') {
                if (job.renders?.length) displayResults(job);
                else showAnalysisStatus(analysisFailureMessage(job));
            }
        }
    } catch (_) {
        showAnalysisStatus('Could not restore the previous job. Check the connection and try again.');
    }
}

function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[char]));
}

function acquisitionFailureMessage(code) {
    const messages = {
        SESSION_EXPIRED: 'Your saved YouTube session has expired. Sign in to YouTube in Safari, run connect-safari-youtube.command in the justClipper folder, then click Retry Make Shorts.',
        INTERRUPTED: 'Download was interrupted by a restart. Click Retry Make Shorts to start a new attempt.',
        BOT_PROTECTION: 'YouTube is blocking automated downloads. Try again later, or upload the original video file to continue.',
        HTTP_429: 'YouTube is limiting download requests. Wait before retrying, or upload the original video file.',
        HTTP_403: 'YouTube denied access to the media stream. Retry later, or upload the original video file.',
        PO_TOKEN_PROVIDER_UNAVAILABLE: 'The local YouTube download helper is unavailable. Restart justClipper and try again.'
    };
    return messages[code] || `Video acquisition failed: ${safeCode(code)}`;
}

function safeCode(value) {
    return /^[A-Z_0-9]{1,64}$/.test(value || '') ? value : 'UNKNOWN';
}

function publishingLimitMessage(code) {
    if (code === 'API_QUOTA_EXCEEDED') return 'YouTube API quota is exhausted.';
    if (code === 'CHANNEL_UPLOAD_LIMIT') return 'The YouTube channel upload limit was reached.';
    if (code === 'RATE_LIMITED') return 'YouTube is rate limiting uploads.';
    if (code === 'QUOTA_EXCEEDED') return 'YouTube reported a publishing limit; its exact type was not recorded.';
    return '';
}

function analysisFailureMessage(job) {
    const providerReason = typeof job === 'string' ? job : job?.error;
    if (providerReason === 'AI_SERVICE_UNAVAILABLE') {
        return 'Gemini was temporarily unavailable during analysis. Your progress was preserved. Retry processing to continue.';
    }
    if (providerReason === 'AI_COMPRESSION_SERVICE_UNAVAILABLE') {
        return 'A strong moment was found, but Gemini was temporarily unavailable while creating the shorter edit. Retry processing to continue.';
    }
    const reason = typeof job === 'string' ? job : job.error;
    if (String(reason || '').startsWith('GEMINI_')) return titleGenerationMessage(reason);
    const code = safeCode(reason);
    if (code === 'ANALYSIS_PROXY_FAILED') {
        return 'Could not prepare the video for analysis. Video encoding failed; details are saved in the local proxy log. You can retry.';
    }
    if (code === 'NO_SUITABLE_CLIPS') {
        return 'Gemini found no complete clips in this pass. Try a smaller target or add specific editorial guidance, then analyze again. Reason: NO_SUITABLE_CLIPS';
    }

    if (code === 'ALL_CANDIDATES_REJECTED') {
        let details = '';
        if (job.candidate_decisions && job.candidate_decisions.length) {
            const reasons = {};
            job.candidate_decisions.forEach(d => {
                const r = d.decision || 'UNKNOWN';
                reasons[r] = (reasons[r] || 0) + 1;
            });
            const parts = Object.entries(reasons).map(([r, count]) => `${count} rejected for ${r}`);
            details = ` ${job.candidate_decisions.length} moments were analyzed, but all were rejected: ${parts.join(', ')}.`;
        }
        return `No clips passed the quality check.${details} Change the settings or guidance and analyze again. Reason: ALL_CANDIDATES_REJECTED`;
    }

    if (code === 'AI_QUOTA_EXHAUSTED') {
        return "Gemini's daily API quota is exhausted. Your completed analysis has been saved. You can continue from this point after the quota resets.";
    }
    if (code === 'AI_INVALID_TIMELINE') {
        return 'AI returned timestamps outside the source video, even after a correction attempt. Analysis could not be validated. Reason: AI_INVALID_TIMELINE';
    }
    return `Analysis failed. Reason: ${code}. You can retry.`;
}

function rightsAllowAcquisition(status) {
    return ['OWNED', 'LICENSED', 'PERMITTED'].includes(status);
}

function rightsExplanation(status) {
    if (status === 'UNKNOWN') return 'Reuse permission has not been verified.';
    if (status === 'RESTRICTED') return 'Acquisition disabled due to rights restrictions.';
    if (status === 'OWNED') return 'Configured creator ownership verified.';
    if (status === 'LICENSED') return 'Reuse license verified.';
    if (status === 'PERMITTED') return 'Explicit permission recorded.';
    return 'Reuse permission has not been verified.';
}

function trendDownloadState(trend) {
    const validId = typeof trend.id === 'string' && /^[A-Za-z0-9_-]{11}$/.test(trend.id);
    const publicSource = trend.privacy_status === 'public' && trend.upload_status === 'processed';
    const eligible = publicSource && (trend.rights_status === 'UNKNOWN' ||
        rightsAllowAcquisition(trend.rights_status));
    const acquisition = trend.acquisition_status || 'AVAILABLE';
    const transientFailure = ['BOT_PROTECTION', 'HTTP_403', 'HTTP_429', 'PO_TOKEN_PROVIDER_UNAVAILABLE', 'NETWORK_ERROR', 'FORMAT_UNAVAILABLE', 'INTERRUPTED', 'SESSION_EXPIRED'].includes(trend.acquisition_error_code);
    const retryable = transientFailure || ['AVAILABLE', 'FAILED', 'COMPLETED', 'UNVERIFIED', 'UNKNOWN'].includes(acquisition);
    return {enabled: validId && eligible && retryable && (trend.technical_downloadability !== 'BLOCKED' || transientFailure),
        eligible, acquisition, retryable};
}

function publicationRightsBlocked(job) {
    const youtubeSource = job.id?.startsWith('job_youtube_') || job.source_id?.startsWith('youtube_');
    return youtubeSource && (job.source_visibility !== 'public' ||
        !['OWNED', 'LICENSED', 'PERMITTED', 'UNKNOWN'].includes(job.rights_status));
}

function publishableJobStatus(job) {
    const renders = job.renders || [];
    return job.status === 'COMPLETED' || (job.status === 'FAILED' &&
        ['INCOMPLETE_RENDER_COUNT', 'RENDER_FAILED', 'DISK_FULL'].includes(job.error) &&
        renders.some(r => r.success));
}

function titleGenerationMessage(code) {
    const messages = {
        GEMINI_UNAVAILABLE: 'Gemini is temporarily overloaded (503). Retry later.',
        GEMINI_QUOTA_EXCEEDED: 'Gemini quota is exhausted. Check the API project quota before retrying.',
        GEMINI_AUTH_REQUIRED: 'Gemini authentication failed. Check the configured API key.',
        GEMINI_PERMISSION_DENIED: 'Gemini rejected access. Check the API project permissions.',
        GEMINI_MODEL_UNAVAILABLE: 'The configured Gemini model is unavailable.',
        GEMINI_TIMEOUT: 'Gemini took too long to process this clip. Retry later.'
    };
    return messages[code] || 'Title generation was unavailable. Retry generation or save an accurate title before publishing.';
}

function needsTitleReview(render) {
    const title = render.youtube_metadata?.title || render.title || '';
    return render.metadata_status === 'REVIEW_REQUIRED' ||
        /^(source[ _]excerpt|timeline[ _]excerpt)(?:\s|$)/i.test(title) ||
        /^(short video|untitled|highlights|generated clip)$/i.test(title);
}

function canPublishRenders(job) {
    const renders = job.renders || [];
    return publishableJobStatus(job) && !publicationRightsBlocked(job) &&
        !renders.some(r => r.publish_status === 'UPLOADING') &&
        renders.some(r => r.success && r.publish_status !== 'UPLOADED' &&
            r.storage_status !== 'CLEANED' && !needsTitleReview(r));
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('btn-import-url')?.addEventListener('click', () => {
        const url = document.getElementById('youtube-url-input').value;
        if (url) importYouTubeUrl(url);
    });
    document.getElementById('btn-make-shorts')?.addEventListener('click', startAutoEdit);

    window.globalClipCount = 'AUTO';
    document.querySelectorAll('input[type="radio"]').forEach(el => el.addEventListener('change', (e) => {
        if (e.target.name === 'clip_count_upload' || e.target.name === 'clip_count_trends') {
            window.globalClipCount = e.target.value;
            document.querySelectorAll(`input[type="radio"][value="${e.target.value}"]`).forEach(r => r.checked = true);
        }
        const desc = document.getElementById('target-count-desc');
        if (e.target.value === 'AUTO') {
            desc.textContent = 'Automatically choose the strongest clips';
        } else {
            if (desc) desc.textContent = `Find up to ${e.target.value} high-quality clips`;
        }
    }));
    checkDiagnostics();
    restoreCurrentJob();

    const dropZone = document.getElementById('drop-zone');
    const fileInput = document.getElementById('video-input');

    dropZone.addEventListener('click', () => fileInput.click());

    fileInput.addEventListener('change', (e) => {
        if(e.target.files.length > 0) {
            uploadVideo(e.target.files[0]);
        }
    });

    document.getElementById('btn-analyze').addEventListener('click', startAnalysis);
    document.getElementById('btn-render').addEventListener('click', startRender);
    document.getElementById('btn-regenerate').addEventListener('click', reopenAnalysisSettings);
    document.getElementById('btn-new-job').addEventListener('click', resetApp);
    document.getElementById('btn-publish-all').addEventListener('click', publishAllToYoutube);
});

async function checkDiagnostics() {
    try {
        const res = await fetch('/api/diagnostics');
        const data = await res.json();
        const panel = document.getElementById('diagnostics-panel');

        let msgs = [];
        if (data.ffmpeg_installed) {
            msgs.push('✓ FFmpeg');
        } else {
            msgs.push('✗ FFmpeg missing');
        }

        if (data.gemini_configured) {
            msgs.push(`✓ Gemini (${data.gemini_model})`);
        } else {
            msgs.push('✗ Gemini API Key missing');
        }

        panel.textContent = msgs.join(' | ');
        if (!data.ffmpeg_installed || !data.gemini_configured) {
            panel.className = 'diagnostics error';
        } else {
            panel.className = 'diagnostics ok';
        }
    } catch (e) {
        document.getElementById('diagnostics-panel').textContent = 'Cannot connect to server';
    }
}

async function responseFailure(res) {
    const data = await res.json().catch(() => ({}));
    if (data.detail === 'PROCESSING_BUSY') return 'Another video is currently being processed. Wait for it to finish before starting a new one.';
    if (data.detail === 'INSUFFICIENT_STORAGE') return 'Not enough server storage to process this video. Safe cleanup was attempted, but the required working space is unavailable. Reason: DISK_FULL';
    if (data.detail === 'SOURCE_FILE_MISSING') return 'The local source file has been cleaned.';
    if (data.detail === 'SOURCE_TOO_SHORT_FOR_TARGET') return 'The source is too short for this many non-overlapping clips at the chosen minimum duration.';
    if (data.detail === 'RIGHTS_UNVERIFIED') return 'Video rights could not be verified. Refresh Trends and try again.';
    return /^[A-Z_0-9]+$/.test(data.detail || '') ? data.detail : `Server error (${res.status})`;
}

async function uploadVideo(file) {
    document.getElementById('drop-zone').innerHTML = '<p>Uploading...</p>';

    try {
        const res = await fetch('/api/upload/stream', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/octet-stream',
                'X-Upload-Filename': encodeURIComponent(file.name),
                'X-Upload-Size': String(file.size)
            },
            body: file
        });

        if (!res.ok) throw new Error(await responseFailure(res));

        const data = await res.json();
        rememberJobId(data.job_id);

        displayMetadata(data.metadata);
        const analyzeButton = document.getElementById('btn-analyze');
        analyzeButton.style.display = '';
        analyzeButton.disabled = false;
        analyzeButton.textContent = 'Analyze with Gemini';
        document.getElementById('analysis-status').style.display = 'none';
        document.getElementById('analysis-progress-container').style.display = 'none';
        showStep('step-settings');

    } catch (e) {
        alert("Upload failed: " + e.message);
        resetApp();
    }
}

function displayMetadata(meta) {
    const div = document.getElementById('metadata-display');
    const sizeMb = (meta.file_size / (1024*1024)).toFixed(2);
    div.innerHTML = `
        <p><strong>File:</strong> ${escapeHtml(meta.filename)}</p>
        <p><strong>Duration:</strong> ${meta.duration.toFixed(2)}s</p>
        <p><strong>Resolution:</strong> ${meta.width}x${meta.height}</p>
        <p><strong>Size:</strong> ${sizeMb} MB</p>
    `;
}

function selectedAutoEditCount() {
    const value = window.globalClipCount || document.getElementById('setting-target-count')?.value || 'AUTO';
    return value === 'AUTO' ? {count_mode:'AUTO', target_clip_count:null}
        : {count_mode:'TARGET', target_clip_count:Number(value)};
}

function qualityCountMessage(job) {
    const found = job.target_metrics?.final_clip_count ?? job.candidates?.length ?? 0;
    const requested = job.analysis_settings?.target_clip_count;
    return requested ? `${found} of ${requested} quality clips found` : `${found} quality clips found`;
}

async function startAutoEdit() {
    if (!currentJobId) { showAnalysisStatus('Upload or select a source video first.'); return; }
    const button = document.getElementById('btn-make-shorts');
    button.disabled = true;
    showAnalysisStatus('Finding stories and editing Shorts…', true);
    try {
        const response = await fetch(`/api/auto-edit/${encodeURIComponent(currentJobId)}`, {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({...selectedAutoEditCount(), editorial_guidance: document.getElementById('setting-guidance').value || null})
        });
        if (!response.ok) throw new Error(await responseFailure(response));
        pollJobStatus('COMPLETED', displayResults);
    } catch (error) {
        showAnalysisStatus(`Could not start Auto Edit: ${error.message}`);
    } finally {
        button.disabled = false;
    }
}

async function startAnalysis() {
    if (!currentJobId) {
        showAnalysisStatus('No active uploaded video. Select a video again, then start analysis.');
        return;
    }

    const settings = {
        clip_count: Number(document.getElementById('setting-clip-count').value),
        exact_clip_count: document.getElementById('setting-exact-count').checked,
        min_clip_seconds: Number(document.getElementById('setting-min-sec').value),
        max_clip_seconds: Number(document.getElementById('setting-max-sec').value),
        editorial_guidance: document.getElementById('setting-guidance').value,
        aspect_mode: document.getElementById('setting-aspect').value,
        output_resolution: document.getElementById('setting-resolution').value
    };

    if (!Number.isInteger(settings.clip_count) || settings.clip_count < 1 || settings.clip_count > 30) {
        showAnalysisStatus('Target clip count must be between 1 and 30. You entered ' +
            document.getElementById('setting-clip-count').value + '.');
        document.getElementById('setting-clip-count').focus();
        return;
    }
    if (!Number.isInteger(settings.min_clip_seconds) || !Number.isInteger(settings.max_clip_seconds) ||
        settings.min_clip_seconds < 5 || settings.max_clip_seconds < 10 ||
        settings.max_clip_seconds < settings.min_clip_seconds) {
        showAnalysisStatus('Enter valid durations: minimum at least 5 seconds, maximum at least 10 seconds, and maximum not below minimum.');
        return;
    }

    const btn = document.getElementById('btn-analyze');
    btn.disabled = true;
    btn.textContent = 'Starting analysis...';
    showAnalysisStatus('Submitting analysis job...', true);

    try {
        const res = await fetch(`/api/analyze/${currentJobId}`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(settings)
        });

        if (!res.ok) throw new Error(await responseFailure(res));

        btn.textContent = 'Analyzing...';
        showAnalysisStatus('Analysis queued. Video understanding may take several minutes.', true);
        pollJobStatus('READY_TO_RENDER', displayCandidates);

    } catch (e) {
        showAnalysisStatus('Analysis could not start: ' + e.message);
        btn.disabled = false;
        btn.textContent = 'Analyze with Gemini';
    }
}


const STATUS_LABELS = {
    "QUEUED_ANALYSIS": "Queued for Analysis",
    "PREPARING_ANALYSIS_MEDIA": "Preparing Source",
    "GENERATING_PROXY": "Compressing Proxy",
    "UPLOADING_TO_GEMINI": "Uploading to AI",
    "WAITING_FOR_GEMINI": "AI Analyzing",
    "UPLOADING_TO_AI": "Uploading to AI",
    "WAITING_FOR_AI_FILE": "Waiting for AI file",
    "ANALYZING_WITH_AI": "Analyzing with AI",
    "BUILDING_CANDIDATES": "Building Candidates",
    "VALIDATING_CANDIDATES": "Selecting Clips",
    "QUEUED_RENDER": "Queued for Render",
    "RENDERING": "Rendering Clips",
    "COMPLETED": "Complete",
    "FAILED": "Failed"
};

function updateJobProgress(job, humanStatus) {
    const p = job.progress;
    const fraction = typeof p === 'number' ? p : (p?.overall_progress ?? p?.progress ?? 0);
    const pct = Math.round(Math.min(1, Math.max(0, Number(fraction) || 0)) * 1000) / 10;
    const message = p?.status_message || p?.message || humanStatus;
    const remaining = p?.estimated_remaining_seconds;
    const eta = job.status === 'FAILED' ? 'Processing stopped' :
        (Number.isFinite(remaining) && remaining > 0 ? `About ${Math.ceil(remaining / 60)} min remaining` :
        (pct === 100 ? 'Completed' : 'Time remaining is not yet known'));
    for (const prefix of ['analysis', 'render']) {
        const setText = (suffix, value) => {
            const element = document.getElementById(`${prefix}-${suffix}-label`);
            if (element) element.textContent = value;
        };
        setText('pct', `${pct}%`);
        setText('stage', (STATUS_LABELS[p?.stage] || humanStatus).toUpperCase());
        setText('msg', message);
        setText('eta', eta);
        const bar = document.getElementById(`${prefix}-progress-bar`);
        if (bar) {
            bar.style.width = `${pct}%`;
            bar.setAttribute('role', 'progressbar');
            bar.setAttribute('aria-valuenow', String(pct));
            bar.setAttribute('aria-valuemin', '0');
            bar.setAttribute('aria-valuemax', '100');
        }
    }
}

function pollJobStatus(targetStatus, callback) {
    if (pollInterval) clearInterval(pollInterval);
    const jobId = currentJobId;
    let inFlight = false;
    let failures = 0;

    const statusMsg = document.getElementById('analysis-status');
    const renderMsg = document.getElementById('render-status');

    const interval = setInterval(async () => {
        if (inFlight || pollInterval !== interval || currentJobId !== jobId) return;
        inFlight = true;
        try {
            const res = await fetch(`/api/status/${encodeURIComponent(jobId)}`);
            if (pollInterval !== interval || currentJobId !== jobId) return;
            if (!res.ok) {
                if (res.status === 404) failures = 3;
                throw new Error(`Status request failed: ${res.status}`);
            }
            const job = await res.json();
            if (pollInterval !== interval || currentJobId !== jobId) return;
            failures = 0;

            let humanStatus = STATUS_LABELS[job.status] || job.status;
            if ((job.status === "QUEUED_ANALYSIS" || job.status === "QUEUED_RENDER") && job.queue_position) {
                humanStatus += ` (Position: ${job.queue_position})`;
            }

            statusMsg.textContent = `Status: ${humanStatus}`;
            renderMsg.textContent = `Status: ${humanStatus}`;

            updateJobProgress(job, humanStatus);
            document.getElementById('btn-make-shorts').disabled =
                !['READY_TO_RENDER', 'COMPLETED', 'FAILED'].includes(job.status);

            if (['READY_TO_RENDER', 'COMPLETED'].includes(job.status)) {
                clearInterval(interval);
                pollInterval = null;
                if (job.status === targetStatus) callback(job);
                else if (job.status === 'COMPLETED') displayResults(job);
                else displayCandidates(job);
            } else if (job.status === 'FAILED') {
                clearInterval(interval);
                pollInterval = null;
                if (targetStatus === 'COMPLETED' && job.renders?.length) {
                    displayResults(job);
                    return;
                }
                statusMsg.style.display = 'block';
                document.getElementById('analysis-progress-container').style.display = 'none';
                statusMsg.textContent = analysisFailureMessage(job);
                if (currentJobId.startsWith('job_')) {
                    const retryButton = document.createElement('button');
                    retryButton.className = 'secondary-btn';
                    retryButton.textContent = 'Retry Processing';
                    retryButton.addEventListener('click', retryJob);
                    statusMsg.appendChild(document.createElement('br'));
                    statusMsg.appendChild(retryButton);
                }
                document.getElementById('btn-analyze').disabled = false;
                document.getElementById('btn-analyze').textContent = 'Analyze with Gemini';
                document.getElementById('btn-render').disabled = false;
            }
        } catch (e) {
            console.error(e);
            if (pollInterval !== interval || currentJobId !== jobId) return;
            failures += 1;
            statusMsg.textContent = 'Cannot read processing status. Reconnecting…';
            if (failures >= 3) {
                clearInterval(interval);
                pollInterval = null;
                showAnalysisStatus('Cannot read processing status. Reload the page to reconnect to the job.');
            }
        } finally {
            inFlight = false;
        }
    }, 2000);
    pollInterval = interval;
}

async function retryJob() {
    if (!currentJobId) return;
    const parts = currentJobId.split('_');
    const sourceId = parts.length > 1 ? parts.slice(1).join('_') : null;

    if (sourceId) {
        document.getElementById('analysis-status').textContent = "Retrying...";
        const res = await fetch(`/api/sources/${sourceId}/process`, { method: 'POST' });
        if(res.ok) {
            pollJobStatus('COMPLETED', displayResults);
        } else {
            alert("Retry failed to start.");
        }
    } else {
        alert("Cannot retry this job automatically.");
    }
}


function displayCandidates(job) {
    showStep('step-candidates');
    const container = document.getElementById('candidates-list');
    container.innerHTML = '';

    if (job.source_summary) {
        const fallbackCount = job.candidates.filter(c => c.selection_origin === 'TIMELINE_FILL').length;
        document.getElementById('source-summary').textContent =
            `${qualityCountMessage(job)}. ${fallbackCount ? `${fallbackCount} timeline excerpts to review. ` : ""}Video Summary: ${job.source_summary}`;
    }

    job.candidates.forEach((c, idx) => {
        const card = document.createElement('div');
        card.className = 'candidate-card';
        card.dataset.idx = idx;

        let segmentsHtml = c.source_segments.map((seg, s_idx) => `
            <div class="segment-edit" data-sidx="${s_idx}">
                <input type="number" class="seg-start" value="${seg.start_seconds}" step="0.1">
                to
                <input type="number" class="seg-end" value="${seg.end_seconds}" step="0.1">
                <span>(${escapeHtml(seg.reason || 'keep')})</span>
            </div>
        `).join('');

        let removedHtml = (c.removed_regions || []).map(r => `
            <li>${r.start_seconds.toFixed(1)}s - ${r.end_seconds.toFixed(1)}s: ${escapeHtml(r.reason)}</li>
        `).join('');
        if (removedHtml) removedHtml = `<ul>${removedHtml}</ul>`;
        else removedHtml = "<em>None</em>";

        let visualHtml = (c.visual_events || []).map(v => `
            <li>${v.start_seconds.toFixed(1)}s - ${v.end_seconds.toFixed(1)}s: <strong>${escapeHtml(v.visual_target_type)}</strong> (${escapeHtml(v.visual_target_description)}) [${escapeHtml(v.preferred_composition)}]</li>
        `).join('');
        if (visualHtml) visualHtml = `<ul>${visualHtml}</ul>`;
        else visualHtml = "<em>None</em>";

        card.innerHTML = `
            <div class="candidate-header">
                <h3>#${c.rank} &middot; ${escapeHtml(c.title)}</h3>
                <span class="score">${c.selection_origin === 'TIMELINE_FILL' ? 'Review required' : `Score: ${c.overall_score || 'N/A'}`}</span>
            </div>
            <div class="candidate-body">
                <p class="short-desc">${escapeHtml(c.short_description || c.hook)}</p>
                ${c.selection_origin === 'TIMELINE_FILL' ? '<p>Timed source excerpt added to reach your requested count. Check it before rendering.</p>' : ''}
                <div class="result-metadata" style="margin-bottom:10px;">
                    <span class="badge topic-badge">${escapeHtml(c.topic || (c.categories ? c.categories[0] : ""))}</span>
                    <span class="badge type-badge">${escapeHtml(c.content_type || 'Unknown')}</span>
                </div>
                <p><strong>Reason:</strong> ${escapeHtml(c.reason)}</p>
            </div>

            <div class="edl-details">
                <div class="edl-section">
                    <h4>Source Segments</h4>
                    <div class="segments-list">${segmentsHtml}</div>
                </div>
                <div class="edl-section">
                    <h4>Removed Regions</h4>
                    ${removedHtml}
                </div>
                <div class="edl-section">
                    <h4>Visual Focus Plan</h4>
                    ${visualHtml}
                </div>
            </div>

            <div class="select-container" style="margin-top: 15px; border-top: 1px solid #444; padding-top: 10px;">
                <label>
                    <input type="checkbox" class="select-clip" checked> Select for rendering
                </label>
            </div>
        `;
        container.appendChild(card);
    });
}

async function startRender() {
    if (!currentJobId) return;

    // Gather selected candidates
    const container = document.getElementById('candidates-list');
    const cards = container.querySelectorAll('.candidate-card');

    // Get job to have base candidate data
    const resJob = await fetch(`/api/status/${currentJobId}`);
    const job = await resJob.json();

    const candidatesToRender = [];

    cards.forEach(card => {
        const isSelected = card.querySelector('.select-clip').checked;
        if (isSelected) {
            const idx = parseInt(card.dataset.idx);
            let c = {...job.candidates[idx]};

            // Read modified segments
            const segElements = card.querySelectorAll('.segment-edit');
            c.source_segments = [];
            segElements.forEach(segEl => {
                const s_idx = parseInt(segEl.dataset.sidx);
                const start = parseFloat(segEl.querySelector('.seg-start').value);
                const end = parseFloat(segEl.querySelector('.seg-end').value);
                let origSeg = job.candidates[idx].source_segments[s_idx];
                c.source_segments.push({
                    start_seconds: start,
                    end_seconds: end,
                    reason: origSeg.reason
                });
            });

            candidatesToRender.push(c);
        }
    });

    if (candidatesToRender.length === 0) {
        alert("Please select at least one clip to render.");
        return;
    }

    const settings = {
        aspect_mode: document.getElementById('setting-aspect').value,
        output_resolution: document.getElementById('setting-resolution').value,
        subtitles: document.getElementById('setting-subtitles').value === "on",
        clip_count: candidatesToRender.length,
        exact_clip_count: job.analysis_settings?.exact_clip_count === true &&
            candidatesToRender.length === job.analysis_settings.clip_count
    };

    const btn = document.getElementById('btn-render');
    btn.disabled = true;

    try {
        const req = {
            filename: job.filename,
            candidates: candidatesToRender,
            settings: settings
        };

        const res = await fetch(`/api/render/${currentJobId}`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(req)
        });

        if (!res.ok) throw new Error(await responseFailure(res));

        pollJobStatus('COMPLETED', displayResults);

    } catch (e) {
        alert("Render failed: " + e.message);
        btn.disabled = false;
    }
}

async function retryRenderOnly() {
    if (!currentJobId) return;
    const button = document.getElementById('btn-retry-render');
    if (button) button.disabled = true;
    try {
        const jobResponse = await fetch(`/api/status/${encodeURIComponent(currentJobId)}`);
        if (!jobResponse.ok) throw new Error(await responseFailure(jobResponse));
        const job = await jobResponse.json();
        const settings = job.render_request?.settings || {};
        const response = await fetch(`/api/render/${encodeURIComponent(currentJobId)}`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({filename: job.filename, candidates: [], settings})
        });
        if (!response.ok) throw new Error(await responseFailure(response));
        showStep('step-candidates');
        document.getElementById('render-status').style.display = 'block';
        document.getElementById('render-status').textContent = 'Retrying unfinished renders...';
        pollJobStatus('COMPLETED', displayResults);
    } catch (error) {
        alert(`Render retry failed: ${error.message}`);
        if (button) button.disabled = false;
    }
}

function displayResults(job) {
    rememberJobId(job.id);
    showStep('step-results');
    const container = document.getElementById('results-list');
    container.innerHTML = '';

    const countSummary = document.createElement('p');
    countSummary.textContent = qualityCountMessage(job);
    container.appendChild(countSummary);

    // We need the original candidate data to show the new metadata
    const candidates = job.candidates || [];
    const renders = job.renders || [];

    const successfulRenders = renders.filter(r => r.success).length;
    if (job.source_summary?.startsWith('AI analysis was unavailable.')) {
        const warning = document.createElement('div');
        warning.className = 'card';
        warning.textContent = 'The source analysis was unavailable, so these are timed excerpts. Titles are generated separately from the rendered videos. If a title is still missing, retry generation below. Titles already uploaded can be edited in YouTube Studio.';
        if (!renders.some(r => r.publish_status === 'UPLOADED')) {
            const analyzeButton = document.createElement('button');
            analyzeButton.className = 'secondary-btn';
            analyzeButton.textContent = 'Retry AI analysis for better clips and titles';
            analyzeButton.addEventListener('click', reopenAnalysisSettings);
            warning.appendChild(analyzeButton);
        }
        container.appendChild(warning);
    }
    if (job.status === 'FAILED' && renders.length) {
        const notice = document.createElement('div');
        notice.className = 'card';
        notice.textContent = `Only ${successfulRenders} of ${renders.length} clips rendered. Ready clips can be published; the source and unfinished clips are kept for retry. Reason: ${safeCode(job.error)}.`;
        const retryButton = document.createElement('button');
        retryButton.id = 'btn-retry-render';
        retryButton.className = 'secondary-btn';
        retryButton.textContent = 'Retry unfinished renders';
        retryButton.addEventListener('click', retryRenderOnly);
        notice.appendChild(retryButton);
        container.appendChild(notice);
    }
    const rightsBlocked = publicationRightsBlocked(job);
    const unknownRights = (job.source_id?.startsWith('youtube_') || job.id?.startsWith('job_youtube_')) &&
        job.rights_status === 'UNKNOWN';
    const bulkButton = document.getElementById('btn-publish-all');
    bulkButton.disabled = bulkPublishingJobId === job.id || !canPublishRenders(job);
    const limitCode = renders.map(r => r.publish_error_code)
        .find(code => publishingLimitMessage(code));
    if (bulkPublishingJobId !== job.id) document.getElementById('publish-all-status').textContent =
        rightsBlocked ? 'Publishing blocked: source is restricted or unavailable.' :
            unknownRights ? 'Reuse permission has not been verified. Attribution does not grant permission.' :
                limitCode ? `${publishingLimitMessage(limitCode)} Unpublished MP4s are kept for a later retry. Reason: ${limitCode}` : '';

    if (renders.length === 0 || successfulRenders === 0) {
        container.insertAdjacentHTML('beforeend', `
            <div style="text-align:center; padding:40px; background:#f9f9f9; border-radius:8px; margin-bottom:20px;">
                <h3 style="color:#f44336; margin-top:0;">No valid clips were rendered.</h3>
                <p style="color:#555;">${escapeHtml(job.error || renders.map(r => r.error).filter(Boolean).join(', ') || 'Unknown rendering failure.')}</p>
                <button onclick="retryRenderOnly()" class="primary-btn" style="margin-top:15px;">Retry Rendering</button>
            </div>
        `);
        return;
    }

    renders.forEach((r, i) => {
        const card = document.createElement('div');
        card.className = 'result-card card';

        // Find corresponding candidate for rich metadata
        const c = candidates.find(cand => cand.rank === r.rank) || {};

        const title = r.youtube_metadata?.title || c.title || r.title;
        const desc = r.generated_description || c.short_description || "Generated clip.";
        const topic = r.generated_topic || (c.topic === "SOURCE_EXCERPT" ? "Timed selection" : c.topic) || "General";
        const contentType = c.content_type === "SOURCE_EXCERPT" ? "Short" : c.content_type || "Video";
        const duration = (c.source_segments || []).reduce((acc, seg) => acc + (seg.end_seconds - seg.start_seconds), 0).toFixed(1);

        if (r.success) {
            const mediaUrl = `/api/jobs/${encodeURIComponent(job.id)}/renders/${r.rank}/media`;
            const uploaded = r.publish_status === 'UPLOADED' && r.youtube_video_id;
            const uploading = r.publish_status === 'UPLOADING';
            const cleaned = r.storage_status === 'CLEANED';
            const youtubeUrl = uploaded ? `https://www.youtube.com/watch?v=${encodeURIComponent(r.youtube_video_id)}` : '';
            const publicationError = safeCode(r.publish_error_code || r.publish_error);
            const publishLabel = r.youtube_metadata?.title || title;
            card.innerHTML = `
                <div class="result-header">
                    <h3>#${r.rank} &middot; ${escapeHtml(title)}</h3>
                </div>
                <div class="result-body">
                    <p class="short-desc">${escapeHtml(desc)}</p>
                    <p class="youtube-title">YouTube title: ${escapeHtml(publishLabel)}</p>
                    ${r.warnings?.length ? `<p>${r.warnings.includes('CAPTIONS_UNAVAILABLE') ? 'Captions could not be generated. This preview has no captions.' : r.warnings.includes('CAPTION_PLACEMENT_REVIEW') ? 'Review caption placement: some captions may overlap important visual details.' : escapeHtml(r.warnings.join(', '))}</p>` : ''}
                    ${r.edit_plan?.style_profile ? `<p>Style: ${escapeHtml(r.edit_plan.style_profile)} · Quality: ${(r.edit_plan.quality_score || 0) >= 70 ? 'HIGH' : (r.edit_plan.quality_score || 0) >= 45 ? 'GOOD' : 'REVIEW'}</p>` : ''}
                    ${r.edit_plan?.decision_trace?.length ? `<details><summary>Editing decisions</summary><ul>${r.edit_plan.decision_trace.map(reason => `<li>${escapeHtml(reason)}</li>`).join('')}</ul></details>` : ''}
                    ${unknownRights ? '<p>Rights: UNKNOWN. Reuse permission has not been verified.</p>' : ''}
                    ${r.youtube_metadata?.hashtags?.length ? `<p>${escapeHtml(r.youtube_metadata.hashtags.join(' '))}</p>` : ''}
                    ${rightsBlocked ? '<p>Publishing blocked by source rights.</p>' : ''}
                    ${r.publish_status === 'FAILED' || r.publish_status === 'REAUTH_REQUIRED' ? `<p>Publish failed. Reason: ${publicationError}</p>` : ''}
                    <div class="result-metadata">
                        <span class="badge topic-badge">${escapeHtml(topic)}</span>
                        <span class="badge type-badge">${escapeHtml(contentType)}</span>
                        <span class="badge duration-badge">${duration}s</span>
                        <span class="badge status-badge success">${uploaded ? (r.youtube_privacy_status === 'public' ? '✓ Published publicly' : r.youtube_privacy_status === 'private' ? '✓ Uploaded to YouTube (private)' : '✓ Uploaded to YouTube (visibility unverified)') : cleaned ? 'Local file expired and was cleaned' : '✓ Ready'}</span>
                    </div>
                </div>
                ${cleaned ? '<p>Local copy cleaned</p>' : `<video src="${mediaUrl}" controls></video>`}
                ${!uploaded && !cleaned ? `<div class="render-title-editor">
                    <label>YouTube title <input class="render-title-input" maxlength="100" value="${escapeHtml(publishLabel)}"></label>
                    <button class="secondary-btn btn-save-title">Save title</button>
                    <button class="secondary-btn btn-generate-title" ${uploading ? 'disabled' : ''}>Generate title from video</button>
                    ${needsTitleReview(r) ? `<p>${escapeHtml(titleGenerationMessage(r.metadata_error))}</p>` : ''}
                    <span class="render-title-status" role="status"></span>
                </div>` : ''}
                <div class="result-footer">
                    ${cleaned ? '' : `<a href="${mediaUrl}" download class="secondary-btn">Download</a>`}
                    ${uploaded ? `<a href="${youtubeUrl}" target="_blank" rel="noopener" class="secondary-btn">Open on YouTube</a>` : cleaned ? '' : `<button class="secondary-btn btn-youtube-upload" ${rightsBlocked || needsTitleReview(r) || !publishableJobStatus(job) || uploading || bulkPublishingJobId === job.id ? 'disabled' : ''} style="background-color: #dc3545; color: white;">${uploading ? 'Uploading...' : 'Publish to YouTube'}</button>`}
                </div>
            `;
            const uploadButton = card.querySelector('.btn-youtube-upload');
            if (uploadButton) uploadButton.addEventListener('click', e =>
                uploadToYoutube(job.id, r.rank, e.currentTarget));
            const generateButton = card.querySelector('.btn-generate-title');
            if (generateButton) generateButton.addEventListener('click', e =>
                generateRenderTitle(job.id, r.rank, e.currentTarget));
            const titleInput = card.querySelector('.render-title-input');
            if (titleInput) titleInput.addEventListener('input', () => {
                if (uploadButton) uploadButton.disabled = true;
                bulkButton.disabled = true;
                card.querySelector('.render-title-status').textContent = 'Save the title before publishing.';
            });
            const saveTitleButton = card.querySelector('.btn-save-title');
            if (saveTitleButton) saveTitleButton.addEventListener('click', e =>
                saveRenderTitle(job.id, r.rank, e.currentTarget));
        } else {
            card.innerHTML = `
                <div class="result-header">
                    <h3>#${r.rank} &middot; ${escapeHtml(title)}</h3>
                </div>
                <div class="result-body">
                    <p class="short-desc">${escapeHtml(desc)}</p>
                    <div class="result-metadata">
                        <span class="badge status-badge error">Failed</span>
                    </div>
                </div>
            `;
        }

        container.appendChild(card);
    });
}

async function generateRenderTitle(jobId, rank, button) {
    const status = button.closest('.render-title-editor').querySelector('.render-title-status');
    button.disabled = true;
    status.textContent = 'Watching this clip and generating a title…';
    try {
        const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/renders/${rank}/metadata/generate`, {method: 'POST'});
        if (!response.ok) throw new Error(await responseFailure(response));
        const result = await response.json();
        if (result.metadata_error || result.metadata_status !== 'GENERATED') throw new Error(titleGenerationMessage(result.metadata_error));
        const current = await fetch(`/api/status/${encodeURIComponent(jobId)}`);
        if (current.ok) displayResults(await current.json());
    } catch (error) {
        status.textContent = error.message;
    } finally {
        button.disabled = false;
    }
}

async function saveRenderTitle(jobId, rank, button) {
    const editor = button.closest('.render-title-editor');
    const title = editor.querySelector('.render-title-input').value.trim();
    const status = editor.querySelector('.render-title-status');
    if (!title) { status.textContent = 'Enter a title.'; return; }
    button.disabled = true;
    status.textContent = 'Saving...';
    try {
        const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/renders/${rank}/title`, {
            method: 'PATCH', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({title})
        });
        if (!response.ok) throw new Error(await responseFailure(response));
        status.textContent = 'Saved. This title will be used when publishing.';
        const current = await fetch(`/api/status/${encodeURIComponent(jobId)}`);
        if (current.ok) displayResults(await current.json());
    } catch (error) {
        status.textContent = `Could not save title: ${error.message}`;
    } finally {
        button.disabled = false;
    }
}

function showStep(stepId) {
    document.querySelectorAll('.step').forEach(s => s.classList.remove('active'));
    document.getElementById(stepId).classList.add('active');
}

function resetApp() {
    forgetJobId();
    if (pollInterval) clearInterval(pollInterval);

    document.getElementById('drop-zone').innerHTML = `
        <input type="file" id="video-input" accept="video/mp4,video/quicktime" />
        <p>Drag & Drop or Click to select (MP4/MOV)</p>
    `;

    document.getElementById('btn-analyze').disabled = false;
    document.getElementById('btn-analyze').style.display = '';
    document.getElementById('btn-analyze').textContent = 'Analyze with Gemini';
    document.getElementById('btn-render').disabled = false;
    document.getElementById('analysis-status').textContent = '';
    document.getElementById('analysis-status').style.display = 'none';
    document.getElementById('analysis-progress-container').style.display = 'none';
    document.getElementById('render-status').textContent = '';
    document.getElementById('render-status').style.display = 'none';
    document.getElementById('render-progress-container').style.display = 'none';

    // Reattach listener to new input
    const dropZone = document.getElementById('drop-zone');
    // clear old clones if any to prevent multiple fires
    const newDrop = dropZone.cloneNode(true);
    dropZone.parentNode.replaceChild(newDrop, dropZone);
    const newFileInput = newDrop.querySelector('input');

    newDrop.addEventListener('click', () => newFileInput.click());
    newFileInput.addEventListener('change', (e) => {
        if(e.target.files.length > 0) uploadVideo(e.target.files[0]);
    });
    showStep('step-upload');
}



// Trends Integration
let trendsNextPage = null;
let trendsLoading = false;
const displayedTrendIds = new Set();
async function loadTrends(forceRefresh = false, append = false) {
    if (trendsLoading || (append && !trendsNextPage)) return;
    trendsLoading = true;
    showStep('step-trends');
    document.getElementById('trends-loading').style.display = 'block';
    if (!append) {
        document.getElementById('trends-grid').innerHTML = '';
        displayedTrendIds.clear();
        trendsNextPage = null;
    }

    try {
        const query = append ? `?page_token=${encodeURIComponent(trendsNextPage)}` : (forceRefresh ? '?force_refresh=true' : '');
        const res = await fetch('/api/trends' + query);
        const data = await res.json();

        document.getElementById('trends-loading').style.display = 'none';

        if (!data.configured) {
            document.getElementById('trends-credential-warning').style.display = 'block';
            return;
        }

        if (data.error) {
            alert("Error loading trends: " + data.error);
            return;
        }

        const grid = document.getElementById('trends-grid');
        trendsNextPage = data.next_page_token || null;
        const more = document.getElementById('btn-load-more-trends');
        if (more) more.style.display = trendsNextPage ? '' : 'none';
        (data.trends || []).forEach(trend => {
            if (displayedTrendIds.has(trend.id)) return;
            displayedTrendIds.add(trend.id);
            const card = document.createElement('div');
            card.className = 'candidate-card card';
            const {enabled, eligible, acquisition, retryable} = trendDownloadState(trend);
            const buttonLabel = trend.rights_status === 'RESTRICTED' ? 'Rights restricted'
                : !eligible ? 'Video unavailable' : trend.technical_downloadability === 'BLOCKED' && !enabled
                    ? 'Acquisition blocked' : acquisition === 'COMPLETED'
                ? 'Make Shorts' : acquisition === 'DOWNLOADING' || acquisition === 'QUEUED'
                    ? 'Downloading...' : acquisition === 'AUTH_REQUIRED'
                        ? 'Authentication required' : acquisition === 'ACQUISITION_BLOCKED' && !enabled
                            ? 'Acquisition blocked' : retryable && acquisition !== 'AVAILABLE'
                            ? 'Retry Make Shorts' : 'Make Shorts';
            const reason = trend.acquisition_error_code
                ? `<p class="dl-reason">${escapeHtml(acquisitionFailureMessage(trend.acquisition_error_code))}</p>` : '';
            card.innerHTML = `
                <div style="position: relative; width: 100%;">
                    <img src="${escapeHtml(trend.thumbnail)}" alt="Thumbnail" style="width:100%; border-radius:5px;">
                    <div style="position: absolute; bottom: 5px; right: 5px; background: rgba(0,0,0,0.8); color: white; padding: 2px 6px; border-radius: 4px; font-size: 0.85em; font-weight: bold;">
                        ${escapeHtml(trend.duration_display || '--:--')}
                    </div>
                    ${trend.content_type && trend.content_type !== 'UNKNOWN' ? `<div style="position: absolute; top: 5px; left: 5px; background: rgba(0,123,255,0.8); color: white; padding: 2px 6px; border-radius: 4px; font-size: 0.8em; font-weight: bold; text-transform: uppercase;">${escapeHtml(trend.content_type)}</div>` : ''}
                </div>
                <h4 style="margin-top:10px;">${escapeHtml(trend.title)}</h4>
                <p style="color:#aaa; font-size:0.9em;">${escapeHtml(trend.channel)} &middot; ${Number(trend.viewCount || 0).toLocaleString()} views${trend.published_at ? ` · ${escapeHtml(trend.published_at.slice(0,10))}` : ''}</p>
                <div style="margin: 10px 0;">
                    <span class="badge topic-badge">${Number.isFinite(trend.views_per_hour) ? `${Number(trend.views_per_hour).toLocaleString()} views/hour · ${trend.momentum_source === 'OBSERVED_GROWTH' ? 'recent snapshot growth' : 'average since upload'}` : 'Collecting momentum data'}</span>
                </div>
                <p class="acquisition-status" style="color:#aaa; font-size:0.85em;">Acquisition: ${escapeHtml(acquisition)}</p>
                <p style="color:#aaa; font-size:0.85em;">Rights: ${escapeHtml(trend.rights_status || 'UNKNOWN')}</p>
                <p style="color:#aaa; font-size:0.85em;">${rightsExplanation(trend.rights_status)}</p>
                <p style="color:#aaa; font-size:0.85em;">Technical downloadability: ${escapeHtml(trend.technical_downloadability || 'UNVERIFIED')}</p>
                <p style="color:#aaa; font-size:0.8em;">Rights evidence: ${safeCode(trend.rights_evidence || trend.rights_reason)}</p>
                ${reason}
                <button class="primary-btn download-trend-btn" ${enabled ? '' : 'disabled'}>${buttonLabel}</button>
                <div class="dl-status" style="margin-top:10px; font-size:0.9em; color:#aaa;"></div>
            `;
            const button = card.querySelector('.download-trend-btn');
            if (enabled) {
                button.dataset.videoId = trend.id;
                button.dataset.title = trend.title || '';
                button.addEventListener('click', () => startDownload(button.dataset.videoId, button.dataset.title, card));
            }
            grid.appendChild(card);
        });

    } catch (e) {
        console.error(e);
        document.getElementById('trends-loading').style.display = 'none';
    } finally { trendsLoading = false; }
}

async function startDownload(videoId, title, cardEl) {
    return beginYouTubeImport({video_id:videoId, title}, cardEl.querySelector('.dl-status'),
                              cardEl.querySelector('.download-trend-btn'));
}

function showNav(navId) {
    document.querySelectorAll('.nav-btn').forEach(b => {
        b.classList.remove('active');
        b.style.color = '#aaa';
        b.style.fontWeight = 'normal';
    });
    const activeBtn = document.getElementById('nav-' + navId);
    if (activeBtn) {
        activeBtn.classList.add('active');
        activeBtn.style.color = '#fff';
        activeBtn.style.fontWeight = 'bold';
    }

    if (navId === 'clipper') {
        showStep('step-upload');
    } else if (navId === 'trends') {
        showStep('step-trends');
        loadTrends();
    } else if (navId === 'library') {
        showStep('step-library');
        loadLibrary();
    } else {
        document.querySelectorAll('.step').forEach(s => s.classList.remove('active'));
        alert("This feature is a placeholder and will be implemented in future updates.");
        showNav('clipper');
    }
}


// --- LIBRARY LOGIC ---
async function loadLibrary() {
    document.getElementById('library-loading').style.display = 'block';
    const grid = document.getElementById('library-grid');
    grid.innerHTML = '';

    try {
        const res = await fetch('/api/sources');
        const data = await res.json();
        const sources = data.sources || [];

        sources.forEach(source => {
            const card = document.createElement('div');
            card.className = 'candidate-card card';

            let thumbHtml = '';
            if (source.thumbnail_path) {
                // Just use the API or object URL for now? Wait, we don't have a thumbnail endpoint.
                // We'll skip thumbnail for now or just put a generic box.
                thumbHtml = `<div style="width:100%; height:120px; background:#222; margin-bottom:10px; display:flex; align-items:center; justify-content:center; color:#555;">[ Thumbnail ]</div>`;
            } else {
                thumbHtml = `<div style="width:100%; height:120px; background:#222; margin-bottom:10px; display:flex; align-items:center; justify-content:center; color:#555;">[ No Thumbnail ]</div>`;
            }

            const durMin = Math.floor(source.duration / 60);
            const durSec = Math.floor(source.duration % 60).toString().padStart(2, '0');

            card.innerHTML = `
                ${thumbHtml}
                <h4>${escapeHtml(String(source.title || "Untitled").substring(0, 50))}...</h4>
                <p style="color:#aaa; font-size:0.8em; margin:5px 0;">Provider: ${escapeHtml(source.provider)}</p>
                <p style="color:#aaa; font-size:0.8em; margin:5px 0;">Duration: ${durMin}:${durSec}</p>

                <div style="margin-top: 10px; font-size: 0.85em;">
                    <p style="margin:2px 0;">SOURCE: <span style="color:#4CAF50;">${escapeHtml(source.acquisition_status)}</span></p>
                    <p style="margin:2px 0;">Rights: ${escapeHtml(source.rights_status || 'UNKNOWN')}</p>
                    <p style="margin:2px 0;">${rightsExplanation(source.rights_status)}</p>
                    <p style="margin:2px 0;">LIBRARY: <span style="color:#2196F3;">${escapeHtml(source.library_status || source.processing_status)}</span></p>
                </div>

                <div style="display:flex; flex-wrap:wrap; gap:5px; margin-top:15px;">
                    <button class="secondary-btn btn-preview" data-id="${escapeHtml(source.source_id)}">Preview</button>
                    <button class="primary-btn btn-process" data-id="${escapeHtml(source.source_id)}" ${(source.provider === 'youtube' && !(rightsAllowAcquisition(source.rights_status) || (source.rights_status === 'UNKNOWN' && source.source_visibility === 'public'))) || source.library_status === 'PROCESSING' ? 'disabled' : ''}>Make Shorts</button>
                    <button class="secondary-btn btn-view" data-id="${escapeHtml(source.source_id)}">View Clips</button>
                </div>
            `;
            grid.appendChild(card);
        });

        document.querySelectorAll('.btn-preview').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const id = e.target.dataset.id;
                const mediaUrl = `/api/sources/${id}/media`;
                window.open(mediaUrl, '_blank'); // Simple preview for now
            });
        });

        document.querySelectorAll('.btn-view').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                const id = e.target.dataset.id;
                const jobId = 'job_' + id;
                try {
                    const res = await fetch(`/api/download/${dlId}`);
                    if (res.ok) {
                        const job = await res.json();
                        if (job.status === 'COMPLETED' || (job.renders && job.renders.length > 0)) {
                            rememberJobId(jobId);
                            displayResults(job);
                        } else {
                            alert("Clips are not ready yet (Status: " + job.status + ").");
                        }
                    } else {
                        alert("No clips found for this video.");
                    }
                } catch (err) {
                    console.error(err);
                }
            });
        });

        document.querySelectorAll('.btn-process').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                const sourceId = e.target.dataset.id;
                // create job from existing source
                e.target.disabled = true;
                e.target.textContent = "Processing...";

                try {
                    const res = await fetch(`/api/sources/${sourceId}/process`, { method: 'POST' });
                    if(res.ok) {
                        const data = await res.json();
                        rememberJobId(data.job_id);

                        showStep('step-settings');
                        document.getElementById('btn-analyze').style.display = 'none';

                        document.getElementById('analysis-progress-container').style.display = 'block';
                        document.getElementById('analysis-status').style.display = 'block';
                        document.getElementById('render-progress-container').style.display = 'block';
                        document.getElementById('render-status').style.display = 'block';


                        pollJobStatus('COMPLETED', displayResults);
                    } else {
                        alert(await responseFailure(res));
                        e.target.disabled = false;
                        e.target.textContent = "Create Clips";
                    }
                } catch (error) {
                    alert(`Could not start processing: ${error.message}`);
                    e.target.disabled = false;
                    e.target.textContent = 'Create Clips';
                }
            });
        });

    } catch (e) {
        console.error(e);
    } finally {
        document.getElementById('library-loading').style.display = 'none';
    }
}

async function checkYoutubeStatus() {
    try {
        const res = await fetch('/api/youtube/status');
        const data = await res.json();
        const statusEl = document.getElementById('youtubeStatus');
        const btnConnect = document.getElementById('btnConnectYoutube');
        const btnDisconnect = document.getElementById('btnDisconnectYoutube');

        if(!statusEl) return;

        if (data.status === 'CONNECTED') {
            statusEl.innerHTML = `<span style="background-color: #198754; color: white; padding: 0.25rem 0.5rem; border-radius: 4px; font-size: 0.8rem;">Connected</span> <span style="margin-left: 0.5rem; font-weight: bold;">${escapeHtml(data.channel_title)}</span>`;
            btnConnect.style.display = 'none';
            btnDisconnect.style.display = 'inline-block';
        } else if (data.status === 'REAUTH_REQUIRED') {
            statusEl.innerHTML = `<span style="background-color: #ffc107; color: black; padding: 0.25rem 0.5rem; border-radius: 4px; font-size: 0.8rem;">Reauthorization Required</span>`;
            btnConnect.style.display = 'inline-block';
            btnConnect.innerText = 'Reconnect YouTube';
            btnDisconnect.style.display = 'inline-block';
        } else {
            statusEl.innerHTML = `<span style="background-color: #6c757d; color: white; padding: 0.25rem 0.5rem; border-radius: 4px; font-size: 0.8rem;">Not Connected</span>`;
            btnConnect.style.display = 'inline-block';
            btnDisconnect.style.display = 'none';
        }
    } catch (e) {
        console.error("Failed to check YT status", e);
    }
}

async function disconnectYoutube() {
    if(!confirm("Are you sure you want to disconnect YouTube?")) return;
    try {
        await fetch('/api/youtube/disconnect', {method: 'POST'});
        checkYoutubeStatus();
    } catch (e) {
        alert("Failed to disconnect");
    }
}

async function uploadToYoutube(jobId, rank, btn) {
    btn.disabled = true;
    btn.innerText = `Uploading...`;

    try {
        const res = await fetch(`/api/youtube/publish/${encodeURIComponent(jobId)}/${rank}`, {method: 'POST'});
        const data = await res.json();
        if(res.ok) {
            alert(data.message || "Upload successful!");
        } else {
            alert("Upload failed. Reason: " + safeCode(data.detail));
        }
    } catch (e) {
        alert("Upload failed. Reason: NETWORK_ERROR");
    } finally {
        try {
            const jobRes = await fetch(`/api/status/${encodeURIComponent(jobId)}`);
            if (jobRes.ok) displayResults(await jobRes.json());
        } catch (_) {
            // The persisted job can be reloaded later.
        }
        btn.disabled = false;
        btn.innerText = `Publish to YouTube`;
    }
}

function showBulkProgress(job) {
    const renders = (job.renders || []).filter(r => r.success);
    const completed = renders.filter(r => r.publish_status === 'UPLOADED').length;
    const active = renders.find(r => r.publish_status === 'UPLOADING');
    const position = active ? renders.indexOf(active) + 1 : Math.min(completed + 1, renders.length);
    const lines = renders.map(r => `#${r.rank} ${r.publish_status === 'UPLOADED' ? 'Uploaded ✓' :
        r.publish_status === 'UPLOADING' ? 'Uploading...' :
        ['FAILED', 'REAUTH_REQUIRED'].includes(r.publish_status)
            ? 'Failed: ' + safeCode(r.publish_error_code) : 'Waiting...'}`);
    document.getElementById('publish-all-status').textContent =
        `Publishing ${position} / ${renders.length}...\n${lines.join('\n')}`;
}

async function publishAllToYoutube() {
    const jobId = currentJobId;
    if (!jobId || bulkPublishingJobId) return;
    bulkPublishingJobId = jobId;
    const button = document.getElementById('btn-publish-all');
    button.disabled = true;
    const statusEl = document.getElementById('publish-all-status');
    let canPublishAgain = false;
    let unfinishedRenders = 0;
    statusEl.textContent = 'Publishing 1 / ...';
    const progressTimer = setInterval(async () => {
        try {
            const response = await fetch(`/api/status/${encodeURIComponent(jobId)}`);
            if (response.ok) showBulkProgress(await response.json());
        } catch (_) {
            // The in-flight request continues; the final response reports every render.
        }
    }, 1500);
    try {
        const response = await fetch(`/api/youtube/publish-all/${encodeURIComponent(jobId)}`, {method: 'POST'});
        const result = await response.json();
        if (!response.ok) throw new Error(safeCode(result.detail));
        clearInterval(progressTimer);
        bulkPublishingJobId = null;
        const jobResponse = await fetch(`/api/status/${encodeURIComponent(jobId)}`);
        if (jobResponse.ok) {
            const updatedJob = await jobResponse.json();
            canPublishAgain = canPublishRenders(updatedJob);
            unfinishedRenders = (updatedJob.renders || []).filter(r => !r.success).length;
            displayResults(updatedJob);
        }
        const total = result.results.filter(item => item.status !== 'SKIPPED').length;
        const uploaded = result.results.filter(item =>
            item.status === 'UPLOADED' || item.status === 'ALREADY_UPLOADED').length;
        const allPublic = result.results.filter(item =>
            item.status === 'UPLOADED' || item.status === 'ALREADY_UPLOADED').every(item =>
            item.privacy_status === 'public');
        const lines = result.results.map(item => `#${item.rank} ${item.status}` +
            (item.error_code ? `: ${safeCode(item.error_code)}` : ''));
        const limitCode = result.results.map(item => item.error_code)
            .find(code => publishingLimitMessage(code));
        const deferred = result.results.filter(item => item.status === 'DEFERRED').length;
        statusEl.textContent = result.status === 'NOTHING_TO_PUBLISH'
            ? `Nothing new to publish.\n${lines.join('\n')}`
            : unfinishedRenders > 0 && uploaded > 0
            ? `${uploaded} ready Shorts uploaded. ${unfinishedRenders} clips still need rendering.\n${lines.join('\n')}`
            : uploaded === total && total > 0 && allPublic
            ? `All ${total} Shorts published successfully.\n${lines.join('\n')}`
            : limitCode
            ? `${uploaded} of ${total} Shorts uploaded. ${publishingLimitMessage(limitCode)} ${deferred} clips were not attempted; their MP4s were kept. Retry when the limit clears.\n${lines.join('\n')}`
            : `${uploaded} of ${total} Shorts uploaded to YouTube. Check visibility on YouTube.\n${lines.join('\n')}`;
    } catch (error) {
        statusEl.textContent = `Publishing failed. Reason: ${safeCode(error.message)}`;
        try {
            const jobResponse = await fetch(`/api/status/${encodeURIComponent(jobId)}`);
            if (jobResponse.ok) {
                const updatedJob = await jobResponse.json();
                canPublishAgain = canPublishRenders(updatedJob);
                bulkPublishingJobId = null;
                displayResults(updatedJob);
            }
        } catch (_) {
            // Retry can be offered after reloading the persisted job.
        }
    } finally {
        clearInterval(progressTimer);
        bulkPublishingJobId = null;
        button.disabled = !canPublishAgain;
    }
}

document.addEventListener("DOMContentLoaded", () => {
    checkYoutubeStatus();
});

let acquisitionInterval = null;
let stopAcquisitionPolling = null;
async function importYouTubeUrl(url) {
    return beginYouTubeImport({url}, document.getElementById('url-error'),
                              document.getElementById('btn-import-url'));
}

async function beginYouTubeImport(source, statusEl, button) {
    if (stopAcquisitionPolling) stopAcquisitionPolling();
    const options = selectedAutoEditCount();
    button.disabled = true;
    statusEl.style.display = 'block';
    statusEl.textContent = 'Validating video and checking Library…';
    try {
        const response = await fetch('/api/download/youtube', {
            method:'POST', signal:AbortSignal.timeout(15000), headers:{'Content-Type':'application/json'},
            body:JSON.stringify({...source, ...options, auto_edit:true})
        });
        if (!response.ok) throw new Error(await responseFailure(response));
        const data = await response.json();
        if (typeof data.dl_id !== 'string') throw new Error('INVALID_ACQUISITION_RESPONSE');
        followAcquisition(data.dl_id, statusEl, button);
    } catch (error) {
        statusEl.textContent = `Video import could not start: ${error.message}`;
        button.disabled = false;
    }
}

function followAcquisition(id, statusEl, button) {
    if (acquisitionInterval) clearInterval(acquisitionInterval);
    let inFlight = false, failures = 0, polls = 0;
    let stopped = false;
    const stop = () => {stopped = true; clearInterval(acquisitionInterval); acquisitionInterval = null; button.disabled = false; stopAcquisitionPolling = null;};
    stopAcquisitionPolling = stop;
    const poll = async () => {
        if (inFlight || stopped) return;
        inFlight = true;
        try {
            if (++polls > 1800) throw new Error('ACQUISITION_STATUS_TIMEOUT');
            const response = await fetch(`/api/download/${encodeURIComponent(id)}`, {signal:AbortSignal.timeout(15000)});
            if (!response.ok) throw new Error(await responseFailure(response));
            const state = await response.json();
            if (stopped) return;
            if (!state.status) throw new Error('Download task is no longer available. Retry the video.');
            failures = 0;
            if (state.status === 'COMPLETED') {
                stop();
                rememberJobId(state.job_id);
                const jobResponse = await fetch(`/api/status/${encodeURIComponent(state.job_id)}`);
                if (!jobResponse.ok) throw new Error(await responseFailure(jobResponse));
                const job = await jobResponse.json();
                if (job.metadata) displayMetadata(job.metadata);
                applyAnalysisSettings(job.analysis_settings);
                if (job.status === 'COMPLETED') {displayResults(job); return;}
                if (job.status === 'READY_TO_RENDER') {displayCandidates(job); return;}
                if (job.status === 'FAILED') {showAnalysisStatus(analysisFailureMessage(job)); return;}
                statusEl.textContent = state.strategy === 'CACHED' ? 'Library source reused. Auto Edit queued.' : 'Source ready. Auto Edit queued.';
                showStep('step-settings');
                showAnalysisStatus('Preparing source for Auto Edit…', true);
                pollJobStatus('READY_TO_RENDER', displayCandidates);
            } else if (['FAILED','AUTH_REQUIRED','ACQUISITION_BLOCKED','RIGHTS_BLOCKED','UNAVAILABLE','INTERRUPTED','CANCELLED','TIMEOUT'].includes(state.status)) {
                stop();
                statusEl.textContent = acquisitionFailureMessage(state.error_code || state.status);
            } else if (state.status === 'DOWNLOADING') {
                const fraction = Number(state.progress);
                statusEl.textContent = Number.isFinite(fraction) && fraction > 0
                    ? `Downloading video: ${Math.round(fraction * 100)}%` : 'Connecting to YouTube and checking video formats…';
            } else {
                statusEl.textContent = 'Video acquisition queued…';
            }
        } catch (error) {
            statusEl.textContent = `Cannot read import status: ${error.message}`;
            if (++failures >= 3 || polls > 1800) stop();
        } finally {inFlight = false;}
    };
    acquisitionInterval = setInterval(poll, 1000);
    poll();
}
