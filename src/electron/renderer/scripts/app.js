// Black Magic Converter - Renderer Application Logic (v4.0)

document.addEventListener('DOMContentLoaded', async () => {
  const electron = window.electronAPI;

  if (!electron) {
    console.error('Electron API not available on window.electronAPI');
    return;
  }

  // UI Element Selectors
  const rootPathEl = document.getElementById('display-root-path');
  const btnSelectRoot = document.getElementById('btn-select-root');
  const servicePill = document.getElementById('service-status-pill');
  const serviceStatusText = document.getElementById('service-status-text');
  const btnToggleService = document.getElementById('btn-toggle-service');
  const btnToggleLabel = document.getElementById('btn-toggle-label');
  const playIcon = btnToggleService.querySelector('.play-icon');
  const stopIcon = btnToggleService.querySelector('.stop-icon');

  const progressContainer = document.getElementById('progress-container');
  const currentClipLabel = document.getElementById('current-clip-name');
  const telemetryFps = document.getElementById('telemetry-fps');
  const telemetryPercent = document.getElementById('telemetry-percent');
  const progressFill = document.getElementById('progress-bar-fill');

  const terminalStream = document.getElementById('terminal-stream');
  const chkAutoScroll = document.getElementById('chk-autoscroll');
  const btnCopyLogs = document.getElementById('btn-copy-logs');
  const btnClearLogs = document.getElementById('btn-clear-logs');

  // Camera Ingest Bar Selectors (Tool 1)
  const cameraStatusPill = document.getElementById('camera-status-pill');
  const cameraStatusLabel = document.getElementById('camera-status-label');
  const barCameraIp = document.getElementById('bar-camera-ip');
  const btnCameraConnect = document.getElementById('btn-camera-connect');
  const chkCameraAutoTransfer = document.getElementById('chk-camera-auto-transfer');
  const btnCameraImportToday = document.getElementById('btn-camera-import-today');
  const cameraDownloadStrip = document.getElementById('camera-download-strip');
  const camDownloadFilename = document.getElementById('cam-download-filename');
  const camDownloadTelemetry = document.getElementById('cam-download-telemetry');
  const camDownloadFill = document.getElementById('cam-download-fill');

  // Modal Selectors
  const btnOpenSettings = document.getElementById('btn-open-settings');
  const modalSettings = document.getElementById('modal-settings');
  const btnCloseSettings = document.getElementById('btn-close-settings');
  const btnCancelSettings = document.getElementById('btn-cancel-settings');
  const btnSaveSettings = document.getElementById('btn-save-settings');

  const cfgLut = document.getElementById('cfg-lut');
  const cfgCodec = document.getElementById('cfg-codec');
  const cfgProfile = document.getElementById('cfg-profile');
  const cfgAudio = document.getElementById('cfg-audio');
  const cfgInterval = document.getElementById('cfg-interval');
  const cfgChecks = document.getElementById('cfg-checks');
  const cfgDelay = document.getElementById('cfg-delay');

  const cfgCamIp = document.getElementById('cfg-cam-ip');
  const cfgCamFtp = document.getElementById('cfg-cam-ftp');
  const btnTestCameraPing = document.getElementById('btn-test-camera-ping');
  const camPingStatusText = document.getElementById('cam-ping-status-text');

  // Application State
  let isRunning = false;
  let currentServiceState = 'idle'; // 'idle' | 'starting' | 'watching' | 'transcoding' | 'stopping' | 'error'
  let activeConfig = {};

  // Connect to camera routine
  async function connectToCamera(targetIp, targetFtp) {
    const ip = (targetIp || (barCameraIp ? barCameraIp.value : '') || (cfgCamIp ? cfgCamIp.value : '') || '192.168.1.118').trim();
    const ftp = (targetFtp || (cfgCamFtp ? cfgCamFtp.value : '') || 'ftp://PYXIS-6K.local').trim();
    if (!ip) return;

    if (barCameraIp) barCameraIp.value = ip;
    if (cfgCamIp) cfgCamIp.value = ip;
    activeConfig.camera_ip = ip;
    activeConfig.camera_ftp = ftp;

    if (cameraStatusPill) {
      cameraStatusPill.className = 'camera-pill connecting';
      cameraStatusLabel.textContent = `Connecting ${ip}...`;
    }
    if (btnCameraConnect) {
      btnCameraConnect.disabled = true;
      btnCameraConnect.textContent = 'Connecting...';
    }
    if (camPingStatusText) {
      camPingStatusText.style.color = 'var(--text-dim)';
      camPingStatusText.textContent = `Connecting to ${ip}...`;
    }

    appendLogLine(`[CAMERA] Connecting to ${ip}...`, 'system');

    try {
      if (electron.camera && electron.camera.connect) {
        await electron.camera.connect({ camera_ip: ip, camera_ftp: ftp });
      } else if (electron.camera) {
        await electron.camera.saveConfig({ camera_ip: ip, camera_ftp: ftp });
        await electron.camera.testConnection({ camera_ip: ip, camera_ftp: ftp });
      }
    } catch (err) {
      appendLogLine(`[CAMERA ERROR] Connection error: ${err.message}`, 'error');
      if (cameraStatusPill) {
        cameraStatusPill.className = 'camera-pill offline';
        cameraStatusLabel.textContent = 'Camera Offline';
      }
    } finally {
      if (btnCameraConnect) {
        btnCameraConnect.disabled = false;
        btnCameraConnect.innerHTML = `<svg viewBox="0 0 24 24" width="11" height="11" stroke="currentColor" stroke-width="2.5" fill="none"><path d="M5 12h14"></path><path d="M12 5l7 7-7 7"></path></svg> Connect`;
      }
    }
  }

  // Initialize
  try {
    const initialState = await electron.getInitialState();
    if (initialState) {
      updateRootDisplay(initialState.rootFolder);
      updateFolderCounts(initialState.counts);
      activeConfig = initialState.config || {};
      populateSettingsForm(activeConfig);
      if (initialState.isRunning) {
        setServiceState('watching');
      } else {
        setServiceState('idle');
      }
    }

    // Initialize Camera State
    if (electron.camera) {
      const camCfg = await electron.camera.getConfig();
      if (camCfg) {
        const ip = camCfg.camera_ip || '192.168.1.118';
        if (cfgCamIp) cfgCamIp.value = ip;
        if (barCameraIp) barCameraIp.value = ip;
        if (cfgCamFtp) cfgCamFtp.value = camCfg.camera_ftp || 'ftp://PYXIS-6K.local';
        if (chkCameraAutoTransfer) chkCameraAutoTransfer.checked = !!camCfg.camera_auto_transfer;
        activeConfig.camera_ip = ip;
      }
      const camStatus = await electron.camera.getStatus();
      if (camStatus) updateCameraStatusUI(camStatus);
    }
  } catch (err) {
    appendLogLine(`[ERROR] Failed to load initial state: ${err.message}`, 'error');
  }

  // Periodic Folder Count Polling (every 3 seconds)
  setInterval(async () => {
    try {
      const counts = await electron.scanFolderCounts();
      updateFolderCounts(counts);
    } catch (e) {
      // ignore
    }
  }, 3000);

  // Periodic Camera Status Polling (every 5 seconds)
  if (electron.camera) {
    setInterval(async () => {
      try {
        const camStatus = await electron.camera.getStatus();
        if (camStatus) updateCameraStatusUI(camStatus);
      } catch (e) {
        // ignore
      }
    }, 5000);
  }

  // Event Listeners from Main Process
  electron.onLog((line) => {
    let type = 'info';
    if (line.includes('[ERROR]') || line.includes('Error:')) type = 'error';
    else if (line.includes('[WARNING]') || line.includes('Warning:')) type = 'warn';
    else if (line.includes('[GUI]') || line.includes('[SYSTEM]') || line.includes('[READY]')) type = 'system';
    else if (line.includes('[CAMERA]')) type = 'render';
    else if (line.includes('Progress:') || line.includes('Render') || line.includes('Speed:')) type = 'render';
    else if (line.includes('Found new clip') || line.includes('Stable file')) type = 'file';

    appendLogLine(line, type);
  });

  electron.onStatus((status) => {
    if (status.state) {
      setServiceState(status.state);
    }
    if (status.currentClip) {
      currentClipLabel.textContent = status.currentClip;
      progressContainer.classList.add('visible');
    } else if (status.state === 'watching' || status.state === 'idle' || status.state === 'stopped') {
      progressContainer.classList.remove('visible');
    }
  });

  electron.onProgress((data) => {
    if (data && typeof data.progress === 'number') {
      progressFill.style.width = `${data.progress}%`;
      telemetryPercent.textContent = `${data.progress}%`;
      progressContainer.classList.add('visible');
    }
  });

  electron.onSpeed((data) => {
    if (data && typeof data.fps === 'number') {
      telemetryFps.textContent = `${data.fps.toFixed(1)} fps`;
    }
  });

  electron.onFolderCounts((counts) => {
    updateFolderCounts(counts);
  });

  // Camera Event Listeners
  if (electron.camera) {
    electron.camera.onStatus((status) => {
      updateCameraStatusUI(status);
    });

    electron.camera.onProgress((p) => {
      if (cameraDownloadStrip) {
        cameraDownloadStrip.classList.add('visible');
        camDownloadFilename.textContent = `Downloading ${p.file_name || ''}`;
        camDownloadTelemetry.textContent = `${p.percent || 0}% (${p.speed_mbps || 0} MB/s | ETA: ${p.eta_seconds || 0}s)`;
        camDownloadFill.style.width = `${p.percent || 0}%`;
      }
    });

    electron.camera.onCompleted((res) => {
      if (cameraDownloadStrip) {
        cameraDownloadStrip.classList.remove('visible');
      }
      appendLogLine(`[CAMERA] Completed download for ${res.file_name} (${res.speed_mbps} MB/s)`, 'render');
    });

    electron.camera.onRecord((rec) => {
      if (cameraStatusPill) {
        if (rec.recording) {
          cameraStatusPill.className = 'camera-pill recording';
          cameraStatusLabel.textContent = 'Camera Recording 🔴';
        } else {
          cameraStatusPill.className = 'camera-pill online';
          cameraStatusLabel.textContent = 'Camera Standby ⏸';
        }
      }
    });

    electron.camera.onTestResult((res) => {
      if (camPingStatusText) {
        if (res.http_ok && res.ftp_ok) {
          camPingStatusText.style.color = 'var(--color-success)';
          camPingStatusText.textContent = `✅ Connected: ${res.product_name || 'Blackmagic Camera'}`;
          if (cameraStatusPill) {
            cameraStatusPill.className = 'camera-pill online';
            cameraStatusLabel.textContent = `${res.product_name || 'PYXIS 6K'} (Online)`;
          }
        } else {
          camPingStatusText.style.color = 'var(--color-error)';
          camPingStatusText.textContent = `⚠️ Failed: REST: ${res.http_ok ? 'OK' : 'FAIL'}, FTP: ${res.ftp_ok ? 'OK' : 'FAIL'}`;
          if (cameraStatusPill) {
            cameraStatusPill.className = 'camera-pill offline';
            cameraStatusLabel.textContent = 'Camera Offline';
          }
        }
      }
    });
  }

  // Camera Connect UI Actions
  if (btnCameraConnect) {
    btnCameraConnect.addEventListener('click', () => {
      connectToCamera();
    });
  }

  if (barCameraIp) {
    barCameraIp.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        connectToCamera();
      }
    });
  }

  if (chkCameraAutoTransfer && electron.camera) {
    chkCameraAutoTransfer.addEventListener('change', async () => {
      const active = chkCameraAutoTransfer.checked;
      appendLogLine(`[CAMERA] Auto-Ingest ${active ? 'activating (snapshotting baseline)...' : 'deactivated.'}`, 'system');
      const res = await electron.camera.toggleAutoTransfer(active);
      if (res && res.active !== undefined) {
        chkCameraAutoTransfer.checked = res.active;
      }
    });
  }

  if (btnCameraImportToday && electron.camera) {
    btnCameraImportToday.addEventListener('click', async () => {
      appendLogLine('[CAMERA] Triggering retroactive same-day import into 00_IN_INGEST...', 'system');
      await electron.camera.importToday();
    });
  }

  if (btnTestCameraPing && electron.camera) {
    btnTestCameraPing.addEventListener('click', async () => {
      const ip = cfgCamIp.value.trim();
      const ftp = cfgCamFtp.value.trim();
      await connectToCamera(ip, ftp);
    });
  }

  // UI Handlers
  btnSelectRoot.addEventListener('click', async () => {
    if (isRunning) {
      alert('Please stop the active watcher before changing the watch folder.');
      return;
    }
    const res = await electron.selectDirectory();
    if (res && res.path) {
      updateRootDisplay(res.path);
      updateFolderCounts(res.counts);
      appendLogLine(`[GUI] Watch root updated to: ${res.path}`, 'system');
    }
  });

  // Reveal buttons for each of the 5 folders
  document.querySelectorAll('.reveal-btn').forEach((btn) => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const folder = btn.getAttribute('data-folder');
      await electron.revealInFinder(folder);
    });
  });

  // Service Start / Stop Toggle
  btnToggleService.addEventListener('click', async () => {
    if (isRunning) {
      setServiceState('stopping');
      await electron.stopWatcher();
    } else {
      setServiceState('starting');
      const res = await electron.startWatcher();
      if (res && !res.success) {
        setServiceState('error');
        appendLogLine(`[ERROR] Start failed: ${res.message}`, 'error');
      } else {
        setServiceState('watching');
      }
    }
  });

  // Terminal actions
  btnCopyLogs.addEventListener('click', () => {
    const text = terminalStream.innerText;
    navigator.clipboard.writeText(text);
    btnCopyLogs.textContent = 'Copied!';
    setTimeout(() => {
      btnCopyLogs.innerHTML = `<svg viewBox="0 0 24 24" width="13" height="13" stroke="currentColor" stroke-width="2" fill="none"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg> Copy`;
    }, 1500);
  });

  btnClearLogs.addEventListener('click', () => {
    terminalStream.innerHTML = '';
  });

  // Settings Modal Handlers
  btnOpenSettings.addEventListener('click', () => {
    populateSettingsForm(activeConfig);
    modalSettings.classList.remove('hidden');
  });

  btnCloseSettings.addEventListener('click', () => {
    modalSettings.classList.add('hidden');
  });

  btnCancelSettings.addEventListener('click', () => {
    modalSettings.classList.add('hidden');
  });

  btnSaveSettings.addEventListener('click', async () => {
    activeConfig.apply_lut = cfgLut.value;
    activeConfig.codec = cfgCodec.value;
    activeConfig.encoder = cfgProfile.value;
    activeConfig.audio_codec = cfgAudio.value;
    activeConfig.scan_interval_seconds = parseInt(cfgInterval.value, 10) || 5;
    activeConfig.stability_checks = parseInt(cfgChecks.value, 10) || 3;
    activeConfig.stability_delay_seconds = parseInt(cfgDelay.value, 10) || 2;

    const newIp = cfgCamIp.value.trim() || '192.168.1.118';
    const newFtp = cfgCamFtp.value.trim() || 'ftp://PYXIS-6K.local';
    const ipChanged = (newIp !== activeConfig.camera_ip);

    activeConfig.camera_ip = newIp;
    activeConfig.camera_ftp = newFtp;

    await electron.saveConfig(activeConfig);

    if (electron.camera) {
      if (ipChanged) {
        connectToCamera(newIp, newFtp);
      } else {
        await electron.camera.saveConfig({
          camera_ip: activeConfig.camera_ip,
          camera_ftp: activeConfig.camera_ftp
        });
      }
    }

    appendLogLine(`[GUI] Configuration updated: LUT='${activeConfig.apply_lut}', Codec='${activeConfig.codec}', CameraIP='${activeConfig.camera_ip}'`, 'system');
    modalSettings.classList.add('hidden');
  });

  // Helper Functions
  function updateRootDisplay(pathStr) {
    if (pathStr) {
      rootPathEl.textContent = pathStr;
      rootPathEl.title = pathStr;
    }
  }

  function updateFolderCounts(counts) {
    if (!counts) return;
    for (const [key, val] of Object.entries(counts)) {
      const el = document.getElementById(`count-${key}`);
      if (el) {
        el.textContent = val;
      }
    }
  }

  function populateSettingsForm(cfg) {
    if (cfg.apply_lut) cfgLut.value = cfg.apply_lut;
    if (cfg.codec) cfgCodec.value = cfg.codec;
    if (cfg.encoder) cfgProfile.value = cfg.encoder;
    if (cfg.audio_codec) cfgAudio.value = cfg.audio_codec;
    if (cfg.scan_interval_seconds) cfgInterval.value = cfg.scan_interval_seconds;
    if (cfg.stability_checks) cfgChecks.value = cfg.stability_checks;
    if (cfg.stability_delay_seconds) cfgDelay.value = cfg.stability_delay_seconds;

    if (cfgCamIp && cfg.camera_ip) cfgCamIp.value = cfg.camera_ip;
    if (cfgCamFtp && cfg.camera_ftp) cfgCamFtp.value = cfg.camera_ftp;
    if (barCameraIp && cfg.camera_ip && document.activeElement !== barCameraIp) {
      barCameraIp.value = cfg.camera_ip;
    }
  }

  function updateCameraStatusUI(status) {
    if (!cameraStatusPill) return;

    if (status.connecting) {
      cameraStatusPill.className = 'camera-pill connecting';
      cameraStatusLabel.textContent = `Connecting ${status.camera_ip || ''}...`;
      return;
    }

    if (status.connected) {
      const prod = status.product || {};
      const model = prod.productName || prod.deviceName || 'PYXIS 6K';
      if (status.recording) {
        cameraStatusPill.className = 'camera-pill recording';
        cameraStatusLabel.textContent = `${model} (Recording 🔴)`;
      } else {
        cameraStatusPill.className = 'camera-pill online';
        cameraStatusLabel.textContent = `${model} (Online)`;
      }
    } else {
      cameraStatusPill.className = 'camera-pill offline';
      cameraStatusLabel.textContent = 'Camera Offline';
    }

    if (status.camera_ip) {
      activeConfig.camera_ip = status.camera_ip;
      if (barCameraIp && document.activeElement !== barCameraIp) {
        barCameraIp.value = status.camera_ip;
      }
      if (cfgCamIp && document.activeElement !== cfgCamIp) {
        cfgCamIp.value = status.camera_ip;
      }
    }

    const t1 = status.tool1;
    if (t1 && chkCameraAutoTransfer) {
      chkCameraAutoTransfer.checked = !!t1.active;
    }
  }

  function setServiceState(state) {
    currentServiceState = state;
    servicePill.className = `service-pill ${state}`;

    switch (state) {
      case 'idle':
      case 'stopped':
        isRunning = false;
        serviceStatusText.textContent = 'Idle';
        btnToggleLabel.textContent = 'Start Watcher';
        btnToggleService.classList.remove('active-running');
        playIcon.classList.remove('hidden');
        stopIcon.classList.add('hidden');
        progressContainer.classList.remove('visible');
        break;

      case 'starting':
        isRunning = true;
        serviceStatusText.textContent = 'Starting...';
        btnToggleLabel.textContent = 'Stop Watcher';
        btnToggleService.classList.add('active-running');
        playIcon.classList.add('hidden');
        stopIcon.classList.remove('hidden');
        break;

      case 'watching':
        isRunning = true;
        serviceStatusText.textContent = 'Watching Folder';
        btnToggleLabel.textContent = 'Stop Watcher';
        btnToggleService.classList.add('active-running');
        playIcon.classList.add('hidden');
        stopIcon.classList.remove('hidden');
        break;

      case 'transcoding':
        isRunning = true;
        serviceStatusText.textContent = 'Transcoding';
        btnToggleLabel.textContent = 'Stop Watcher';
        btnToggleService.classList.add('active-running');
        playIcon.classList.add('hidden');
        stopIcon.classList.remove('hidden');
        progressContainer.classList.add('visible');
        break;

      case 'stopping':
        isRunning = false;
        serviceStatusText.textContent = 'Stopping...';
        btnToggleLabel.textContent = 'Start Watcher';
        break;

      case 'error':
        isRunning = false;
        serviceStatusText.textContent = 'Engine Error';
        btnToggleLabel.textContent = 'Retry Start';
        btnToggleService.classList.remove('active-running');
        playIcon.classList.remove('hidden');
        stopIcon.classList.add('hidden');
        break;
    }
  }

  function appendLogLine(text, type = 'info') {
    const line = document.createElement('div');
    line.className = `log-line log-${type}`;
    line.textContent = text;
    terminalStream.appendChild(line);

    if (chkAutoScroll.checked) {
      terminalStream.scrollTop = terminalStream.scrollHeight;
    }
  }
});
