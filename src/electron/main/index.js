const { app, BrowserWindow, ipcMain, dialog, shell } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const os = require('os');

// ═══════════════════════════════════════════════════════════════════
// DEBUG INFRASTRUCTURE — File Logger & Startup Diagnostics
// macOS logs:   ~/Library/Logs/BlackMagicConverter/main.log
// Windows logs: %APPDATA%/BlackMagicConverter/logs/main.log
// Linux logs:   ~/.config/BlackMagicConverter/logs/main.log
// ═══════════════════════════════════════════════════════════════════

function getLogDirectory() {
  if (process.platform === 'darwin') {
    return path.join(os.homedir(), 'Library', 'Logs', 'BlackMagicConverter');
  } else if (process.platform === 'win32') {
    return path.join(process.env.APPDATA || os.homedir(), 'BlackMagicConverter', 'logs');
  }
  return path.join(os.homedir(), '.config', 'BlackMagicConverter', 'logs');
}

const LOG_DIR = getLogDirectory();
const LOG_FILE = path.join(LOG_DIR, 'main.log');

try {
  fs.mkdirSync(LOG_DIR, { recursive: true });
  // Truncate log on fresh launch so it doesn't grow unbounded
  fs.writeFileSync(LOG_FILE, '', 'utf8');
} catch (_e) {
  // If we can't create the log dir, we'll still have console output
}

function debugLog(tag, message, data) {
  const timestamp = new Date().toISOString();
  const line = data !== undefined
    ? `[${timestamp}] [${tag}] ${message} ${typeof data === 'string' ? data : JSON.stringify(data)}`
    : `[${timestamp}] [${tag}] ${message}`;
  console.log(line);
  try {
    fs.appendFileSync(LOG_FILE, line + '\n');
  } catch (_e) {
    // Swallow file write errors
  }
}

debugLog('STARTUP', '══════════════════════════════════════════════════');
debugLog('STARTUP', 'Main process loaded');
debugLog('STARTUP', `Electron: ${process.versions.electron} | Node: ${process.versions.node} | Chrome: ${process.versions.chrome}`);
debugLog('STARTUP', `Platform: ${process.platform} ${process.arch}`);
debugLog('STARTUP', `app.getAppPath(): ${app.getAppPath()}`);
debugLog('STARTUP', `process.resourcesPath: ${process.resourcesPath}`);
debugLog('STARTUP', `process.cwd(): ${process.cwd()}`);
debugLog('STARTUP', `__dirname: ${__dirname}`);
debugLog('STARTUP', `app.isPackaged: ${app.isPackaged}`);
debugLog('STARTUP', `Log file: ${LOG_FILE}`);

// Global uncaught exception handler — shows native error dialog
process.on('uncaughtException', (error) => {
  const msg = `Uncaught Exception:\n${error.stack || error.message || String(error)}`;
  debugLog('FATAL', msg);
  try {
    dialog.showErrorBox('Black Magic Converter — Fatal Error',
      `${msg}\n\nLog file: ${LOG_FILE}`);
  } catch (_e) {
    // dialog may not be available if app hasn't initialized yet
  }
});

process.on('unhandledRejection', (reason) => {
  debugLog('FATAL', `Unhandled Promise Rejection: ${reason}`);
});

// Handle creating/removing shortcuts on Windows when installing/uninstalling.
try {
  if (require('electron-squirrel-startup')) {
    app.quit();
  }
  debugLog('STARTUP', 'electron-squirrel-startup check passed');
} catch (e) {
  debugLog('STARTUP', `electron-squirrel-startup not available (expected on non-Windows): ${e.message}`);
}

let mainWindow = null;
let watcherProcess = null;
let currentConfig = {
  scan_interval_seconds: 5,
  stability_checks: 3,
  stability_delay_seconds: 2,
  format: 'mp4',
  codec: 'H265',
  encoder: 'Main10',
  resolution_match_source: true,
  apply_lut: 'Blackmagic Gen 5 to Extended Video',
  audio_codec: 'AAC',
  camera_ip: '192.168.1.118',
  camera_ftp: 'ftp://PYXIS-6K.local',
  camera_auto_transfer: false
};

function getUserConfigPath() {
  try {
    return path.join(app.getPath('userData'), 'user_preferences.json');
  } catch (_e) {
    return path.join(process.cwd(), 'user_preferences.json');
  }
}

function loadUserConfig() {
  try {
    const configPath = getUserConfigPath();
    if (fs.existsSync(configPath)) {
      const saved = JSON.parse(fs.readFileSync(configPath, 'utf8'));
      if (saved && typeof saved === 'object') {
        currentConfig = { ...currentConfig, ...saved };
        debugLog('CONFIG', `Loaded user preferences from ${configPath}: camera_ip=${currentConfig.camera_ip}`);
      }
    }
  } catch (err) {
    debugLog('CONFIG', `Error loading user preferences: ${err.message}`);
  }
}

function saveUserConfig() {
  try {
    const configPath = getUserConfigPath();
    fs.mkdirSync(path.dirname(configPath), { recursive: true });
    fs.writeFileSync(configPath, JSON.stringify(currentConfig, null, 2), 'utf8');
    debugLog('CONFIG', `Saved user preferences to ${configPath}: camera_ip=${currentConfig.camera_ip}`);
  } catch (err) {
    debugLog('CONFIG', `Error saving user preferences: ${err.message}`);
  }
}

loadUserConfig();

const REQUIRED_SUBDIRECTORIES = [
  '00_IN_INGEST',
  '01_PROCESSING',
  '02_COMPLETED_MP4',
  '03_ARCHIVE_BRAW',
  '99_FAILED'
];

function resolveDefaultRootFolder() {
  const appPath = app.getAppPath();
  const defaultWatch = path.join(appPath, 'watch_folders');
  debugLog('PATHS', `resolveDefaultRootFolder: appPath=${appPath}`);
  debugLog('PATHS', `resolveDefaultRootFolder: checking ${defaultWatch}`);
  if (fs.existsSync(defaultWatch)) {
    debugLog('PATHS', `resolveDefaultRootFolder: ✅ found watch_folders at appPath`);
    return defaultWatch;
  }
  const cwdWatch = path.join(process.cwd(), 'watch_folders');
  debugLog('PATHS', `resolveDefaultRootFolder: falling back to CWD: ${cwdWatch}`);
  return cwdWatch;
}

let activeRootFolder = resolveDefaultRootFolder();
debugLog('PATHS', `activeRootFolder = ${activeRootFolder}`);

function createWindow() {
  debugLog('STARTUP', 'createWindow() called');
  mainWindow = new BrowserWindow({
    width: 1100,
    height: 760,
    minWidth: 860,
    minHeight: 600,
    title: 'Black Magic Converter',
    titleBarStyle: 'hiddenInset',
    backgroundColor: '#0f1117',
    icon: path.join(__dirname, '../../../assets/icons/icon.png'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
    },
  });

  // Load Vite Dev Server URL or bundled index.html
  if (MAIN_WINDOW_VITE_DEV_SERVER_URL) {
    debugLog('STARTUP', `Loading dev server URL: ${MAIN_WINDOW_VITE_DEV_SERVER_URL}`);
    mainWindow.loadURL(MAIN_WINDOW_VITE_DEV_SERVER_URL);
  } else {
    const rendererPath = path.join(__dirname, `../renderer/${MAIN_WINDOW_VITE_NAME}/index.html`);
    debugLog('STARTUP', `Loading renderer file: ${rendererPath}`);
    mainWindow.loadFile(rendererPath);
  }

  // Debug: renderer load events
  mainWindow.webContents.on('did-finish-load', () => {
    debugLog('STARTUP', '✅ Renderer did-finish-load');
  });

  mainWindow.webContents.on('did-fail-load', (_event, errorCode, errorDescription, validatedURL) => {
    debugLog('ERROR', `Renderer did-fail-load: code=${errorCode} desc="${errorDescription}" url=${validatedURL}`);
  });

  mainWindow.webContents.on('render-process-gone', (_event, details) => {
    debugLog('FATAL', 'Render process gone', details);
  });

  mainWindow.webContents.on('console-message', (_event, level, message, line, sourceId) => {
    const levelNames = ['DEBUG', 'INFO', 'WARN', 'ERROR'];
    debugLog('RENDERER', `[${levelNames[level] || level}] ${message} (${sourceId}:${line})`);
  });

  mainWindow.on('ready-to-show', () => {
    debugLog('STARTUP', '✅ Window ready-to-show');
  });

  mainWindow.on('closed', () => {
    debugLog('LIFECYCLE', 'Main window closed');
    mainWindow = null;
    stopWatcherProcess();
  });

  mainWindow.show();
  debugLog('STARTUP', '✅ BrowserWindow created and show() called');
}

function validateAndProvisionDirectories(rootFolder) {
  if (!fs.existsSync(rootFolder)) {
    fs.mkdirSync(rootFolder, { recursive: true });
  }
  for (const sub of REQUIRED_SUBDIRECTORIES) {
    const subPath = path.join(rootFolder, sub);
    if (!fs.existsSync(subPath)) {
      fs.mkdirSync(subPath, { recursive: true });
    }
  }
}

function countFilesInDirectory(dirPath) {
  try {
    if (!fs.existsSync(dirPath)) return 0;
    const items = fs.readdirSync(dirPath);
    return items.filter(f => !f.startsWith('.')).length;
  } catch (err) {
    return 0;
  }
}

function scanAllFolderCounts(rootFolder) {
  const counts = {};
  for (const sub of REQUIRED_SUBDIRECTORIES) {
    counts[sub] = countFilesInDirectory(path.join(rootFolder, sub));
  }
  return counts;
}

function resolvePythonBinary() {
  const candidates = [
    process.env.PYTHON_PATH,
    '/Library/Frameworks/Python.framework/Versions/3.12/bin/python3',
    '/Library/Frameworks/Python.framework/Versions/3.11/bin/python3',
    '/opt/homebrew/bin/python3',
    '/usr/local/bin/python3',
    '/usr/bin/python3',
    'python3',
    'python'
  ].filter(Boolean);

  for (const p of candidates) {
    if (p.includes('/') && fs.existsSync(p)) {
      return p;
    }
  }
  return 'python3';
}

function resolveProjectRootDir() {
  const candidates = [
    process.resourcesPath,
    path.join(process.resourcesPath, 'app.asar.unpacked'),
    app.getAppPath(),
    path.dirname(app.getAppPath()),
    process.cwd(),
  ];
  debugLog('PATHS', 'resolveProjectRootDir candidates:', candidates);
  for (const c of candidates) {
    const cliPath = path.join(c, 'src', 'cli.py');
    const exists = fs.existsSync(cliPath);
    debugLog('PATHS', `  checking ${cliPath} → ${exists ? '✅ FOUND' : '✗ not found'}`);
    if (c && exists) {
      debugLog('PATHS', `resolveProjectRootDir: using ${c}`);
      return c;
    }
  }
  debugLog('PATHS', `resolveProjectRootDir: ⚠ no cli.py found, falling back to CWD: ${process.cwd()}`);
  return process.cwd();
}

// ═══════════════════════════════════════════════════════════════════
// Inline YAML Serializer — replaces js-yaml npm dependency entirely.
// Only needs to handle: nested objects, arrays, strings, numbers, booleans.
// Output is consumed by Python's yaml.safe_load().
// ═══════════════════════════════════════════════════════════════════
function serializeToYaml(obj, indent = 0) {
  const prefix = '  '.repeat(indent);
  let output = '';
  for (const [key, value] of Object.entries(obj)) {
    if (value === null || value === undefined) {
      output += `${prefix}${key}: null\n`;
    } else if (Array.isArray(value)) {
      output += `${prefix}${key}:\n`;
      for (const item of value) {
        output += typeof item === 'string'
          ? `${prefix}  - '${item.replace(/'/g, "''")}'
`
          : `${prefix}  - ${item}\n`;
      }
    } else if (typeof value === 'object') {
      output += `${prefix}${key}:\n`;
      output += serializeToYaml(value, indent + 1);
    } else if (typeof value === 'string') {
      output += `${prefix}${key}: '${value.replace(/'/g, "''")}'
`;
    } else {
      // numbers, booleans
      output += `${prefix}${key}: ${value}\n`;
    }
  }
  return output;
}

function generateRuntimeYaml(rootFolder, config) {
  const projectRoot = resolveProjectRootDir();
  const configObj = {
    storage: {
      ingest_dir: path.join(rootFolder, '00_IN_INGEST'),
      processing_dir: path.join(rootFolder, '01_PROCESSING'),
      completed_dir: path.join(rootFolder, '02_COMPLETED_MP4'),
      archive_dir: path.join(rootFolder, '03_ARCHIVE_BRAW'),
      failed_dir: path.join(rootFolder, '99_FAILED')
    },
    watcher: {
      poll_interval: config.scan_interval_seconds || 2.0,
      stability_checks: config.stability_checks || 3,
      stability_delay: config.stability_delay_seconds || 2.0,
      extensions: ['.braw'],
      include_sidecars: true
    },
    transcode: {
      container: config.format || 'mp4',
      codec: config.codec || 'H265',
      encoding_profile: config.encoder || 'Main10',
      video_quality: 'Best',
      bitrate_mbps: 0,
      resolution: config.resolution_match_source !== false ? 'source' : '1080p',
      frame_rate: 'source',
      audio: {
        codec: (config.audio_codec || 'aac').toLowerCase(),
        sample_rate: 48000,
        bit_depth: 16,
        bitrate_kbps: 320
      },
      color: {
        mode: 'lut',
        lut_path: config.apply_lut || 'Blackmagic Gen 5 Film to Extended Video.cube',
        fallback_lut_path: 'Blackmagic Film to Extended Video v4.cube'
      }
    },
    engine: {
      type: 'native',
      ffmpeg_path: 'ffmpeg',
      decoder_path: path.join(projectRoot, 'bin', 'braw_decode'),
      hardware_acceleration: true
    }
  };

  const yamlStr = serializeToYaml(configObj);
  debugLog('ENGINE', `Generated runtime YAML config (${yamlStr.length} bytes)`);
  const tempPath = path.join(os.tmpdir(), 'braw_electron_config.yaml');
  fs.writeFileSync(tempPath, yamlStr, 'utf8');
  return tempPath;
}

function parseStreamLine(rawLine) {
  if (!mainWindow || !rawLine) return;

  // Strip ANSI terminal color codes (e.g. \x1b[32m ... \x1b[0m)
  const line = rawLine.replace(/[\u001b\x1b]\[[0-9;]*[a-zA-Z]/g, '').trim();
  if (!line) return;

  mainWindow.webContents.send('engine:log', line);

  // Parse starting transcode
  const transcodeMatch = line.match(/Starting transcode job for:\s*(.+)$/i);
  if (transcodeMatch) {
    mainWindow.webContents.send('engine:status', {
      state: 'transcoding',
      currentClip: transcodeMatch[1].trim(),
      message: `Transcoding ${transcodeMatch[1].trim()}`
    });
  }

  // Parse progress percentage and speed from decoder/pipeline logs
  const transcodeProgMatch = line.match(/Transcode Progress:\s*([\d\.]+)%\s*\(Frame\s*\d+\/\d+\)\s*Speed:\s*([\d\.]+)\s*fps/i);
  const oldProgMatch = line.match(/Progress:\s*frame\s*\d+\/\d+\s*\(([\d\.]+)%\)\s*@\s*([\d\.]+)\s*fps/i);
  const jsonProgMatch = line.match(/PROGRESS:\{.*"percent":([\d\.]+).*,"fps":([\d\.]+).*\}/i);

  if (transcodeProgMatch) {
    const pct = Math.round(parseFloat(transcodeProgMatch[1]));
    const fps = parseFloat(transcodeProgMatch[2]);
    mainWindow.webContents.send('engine:progress', { progress: pct });
    mainWindow.webContents.send('engine:speed', { fps });
  } else if (oldProgMatch) {
    const pct = Math.round(parseFloat(oldProgMatch[1]));
    const fps = parseFloat(oldProgMatch[2]);
    mainWindow.webContents.send('engine:progress', { progress: pct });
    mainWindow.webContents.send('engine:speed', { fps });
  } else if (jsonProgMatch) {
    const pct = Math.round(parseFloat(jsonProgMatch[1]));
    const fps = parseFloat(jsonProgMatch[2]);
    mainWindow.webContents.send('engine:progress', { progress: pct });
    mainWindow.webContents.send('engine:speed', { fps });
  }

  // Parse completion
  if (line.includes('Transcode succeeded for') || line.includes('Render completed successfully') || line.includes('Finished transcode')) {
    mainWindow.webContents.send('engine:progress', { progress: 100 });
    mainWindow.webContents.send('engine:status', {
      state: 'watching',
      currentClip: null,
      message: 'Transcode completed. Watching folder for new clips...'
    });
    mainWindow.webContents.send('engine:folderCounts', scanAllFolderCounts(activeRootFolder));
  }

  // Parse failure
  if (line.includes('Transcode failed') || line.includes('[ERROR]')) {
    mainWindow.webContents.send('engine:status', {
      message: 'Notice/Error encountered during execution.'
    });
    mainWindow.webContents.send('engine:folderCounts', scanAllFolderCounts(activeRootFolder));
  }
}

function stopWatcherProcess() {
  if (watcherProcess) {
    try {
      watcherProcess.kill('SIGINT');
      const procRef = watcherProcess;
      setTimeout(() => {
        try {
          if (procRef && !procRef.killed) {
            procRef.kill('SIGTERM');
          }
        } catch (e) {
          // ignore
        }
      }, 3000);
    } catch (err) {
      console.error('Error stopping watcher process:', err);
    }
    watcherProcess = null;
  }
}

// ═══════════════════════════════════════════════════════════════════
// Blackmagic Camera Ingest Service (Tool 1: Auto Clip Transfer)
// ═══════════════════════════════════════════════════════════════════
let cameraProcess = null;
let latestCameraStatus = {
  connected: false,
  camera_ip: currentConfig.camera_ip,
  camera_ftp: currentConfig.camera_ftp,
  product: {},
  recording: false,
  tool1: { active: false, total_files: 0, total_bytes: 0 }
};

function startCameraService(autoStartTool1 = false) {
  if (cameraProcess) return;
  const pythonBin = resolvePythonBinary();
  const projectDir = resolveProjectRootDir();
  const ingestDir = path.join(activeRootFolder, '00_IN_INGEST');

  validateAndProvisionDirectories(activeRootFolder);

  const env = {
    ...process.env,
    PYTHONPATH: `${process.env.PYTHONPATH || ''}:${projectDir}`,
    PYTHONUNBUFFERED: '1'
  };

  const args = [
    '-u',
    '-m',
    'src.cli',
    'camera-service',
    '--camera-ip', currentConfig.camera_ip || '192.168.1.118',
    '--camera-ftp', currentConfig.camera_ftp || 'ftp://PYXIS-6K.local',
    '--dest-dir', ingestDir
  ];
  if (autoStartTool1) {
    args.push('--auto-start');
  }

  debugLog('CAMERA', `Spawning camera service: ${pythonBin} ${args.join(' ')}`);

  cameraProcess = spawn(pythonBin, args, { cwd: projectDir, env: env });

  cameraProcess.stdout.on('data', (data) => {
    const lines = data.toString().split('\n');
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        const evt = JSON.parse(line.trim());
        handleCameraEvent(evt);
      } catch (_e) {
        debugLog('CAMERA_RAW', line.trim());
      }
    }
  });

  cameraProcess.stderr.on('data', (data) => {
    debugLog('CAMERA_STDERR', data.toString().trim());
  });

  cameraProcess.on('close', (code) => {
    debugLog('CAMERA', `Camera process exited with code ${code}`);
    cameraProcess = null;
    latestCameraStatus.connected = false;
    if (latestCameraStatus.tool1) latestCameraStatus.tool1.active = false;
    if (mainWindow) {
      mainWindow.webContents.send('camera:status', latestCameraStatus);
    }
  });
}

function stopCameraService() {
  if (cameraProcess) {
    try {
      sendCameraCommand({ cmd: 'quit' });
      const ref = cameraProcess;
      setTimeout(() => {
        try {
          if (ref && !ref.killed) ref.kill('SIGINT');
        } catch (_) {}
      }, 1000);
    } catch (_) {}
    cameraProcess = null;
  }
}

function sendCameraCommand(cmdObj) {
  if (cameraProcess && cameraProcess.stdin && !cameraProcess.stdin.destroyed) {
    try {
      cameraProcess.stdin.write(JSON.stringify(cmdObj) + '\n');
    } catch (e) {
      debugLog('CAMERA_ERR', `Failed to write to stdin: ${e.message}`);
    }
  }
}

function handleCameraEvent(evt) {
  if (!mainWindow) return;
  const { type, data } = evt;

  if (type === 'tool1_transfer_progress') {
    mainWindow.webContents.send('camera:progress', data);
  } else if (type === 'tool1_transfer_completed') {
    mainWindow.webContents.send('camera:completed', data);
    mainWindow.webContents.send('engine:folderCounts', scanAllFolderCounts(activeRootFolder));
    mainWindow.webContents.send('engine:log', `[CAMERA] Transferred ${data.file_name} into 00_IN_INGEST (${data.speed_mbps} MB/s)`);
  } else if (type === 'tool1_transfer_failed') {
    mainWindow.webContents.send('engine:log', `[CAMERA ERROR] Transfer failed for ${data.file_name}: ${data.error}`);
  } else if (type === 'status' || type === 'heartbeat' || type === 'service_ready') {
    latestCameraStatus = { ...latestCameraStatus, ...data };
    mainWindow.webContents.send('camera:status', latestCameraStatus);
  } else if (type === 'tool1_status') {
    if (latestCameraStatus.tool1) {
      latestCameraStatus.tool1 = { ...latestCameraStatus.tool1, ...data };
    }
    mainWindow.webContents.send('camera:status', latestCameraStatus);
  } else if (type === 'camera_record_started') {
    latestCameraStatus.recording = true;
    mainWindow.webContents.send('camera:record', { recording: true });
    mainWindow.webContents.send('engine:log', '[CAMERA] 🔴 Camera recording started...');
  } else if (type === 'camera_record_stopped') {
    latestCameraStatus.recording = false;
    mainWindow.webContents.send('camera:record', { recording: false });
    mainWindow.webContents.send('engine:log', '[CAMERA] ⏸ Camera recording stopped. Finalizing clip and queueing transfer...');
  } else if (type === 'tool1_new_clips_detected') {
    mainWindow.webContents.send('engine:log', `[CAMERA] Detected ${data.new_clips_count} new clip(s) for transfer`);
  } else if (type === 'test_connection_result') {
    mainWindow.webContents.send('camera:testResult', data);
  }
}

// IPC Handlers
ipcMain.handle('dialog:selectDirectory', async () => {
  if (!mainWindow) return null;
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ['openDirectory', 'createDirectory'],
    defaultPath: activeRootFolder,
    title: 'Select BRAW Watch Directory'
  });

  if (!result.canceled && result.filePaths.length > 0) {
    const selected = result.filePaths[0];
    activeRootFolder = selected;
    validateAndProvisionDirectories(activeRootFolder);
    return {
      path: activeRootFolder,
      counts: scanAllFolderCounts(activeRootFolder)
    };
  }
  return null;
});

ipcMain.handle('dialog:revealInFinder', async (_event, subfolderName) => {
  const target = subfolderName
    ? path.join(activeRootFolder, subfolderName)
    : activeRootFolder;

  if (fs.existsSync(target)) {
    shell.openPath(target);
    return true;
  }
  return false;
});

ipcMain.handle('engine:getInitialState', () => {
  validateAndProvisionDirectories(activeRootFolder);
  return {
    rootFolder: activeRootFolder,
    counts: scanAllFolderCounts(activeRootFolder),
    config: currentConfig,
    isRunning: watcherProcess !== null,
    subdirectories: REQUIRED_SUBDIRECTORIES
  };
});

ipcMain.handle('engine:scanFolderCounts', () => {
  return scanAllFolderCounts(activeRootFolder);
});

ipcMain.handle('engine:saveConfig', (_event, newConfig) => {
  currentConfig = { ...currentConfig, ...newConfig };
  saveUserConfig();
  return currentConfig;
});

ipcMain.handle('engine:startWatcher', async () => {
  if (watcherProcess) {
    return { success: false, message: 'Watcher is already running.' };
  }

  try {
    validateAndProvisionDirectories(activeRootFolder);
    const configFilePath = generateRuntimeYaml(activeRootFolder, currentConfig);
    const pythonBin = resolvePythonBinary();
    const projectDir = resolveProjectRootDir();

    const env = {
      ...process.env,
      PYTHONPATH: `${process.env.PYTHONPATH || ''}:${projectDir}`,
      PYTHONUNBUFFERED: '1'
    };

    const args = ['-u', '-m', 'src.cli', 'watch', '--config', configFilePath];

    if (mainWindow) {
      mainWindow.webContents.send('engine:log', `[GUI] Launching Python Watcher: ${pythonBin} ${args.join(' ')}`);
      mainWindow.webContents.send('engine:log', `[GUI] Target Watch Folder: ${activeRootFolder}`);
      mainWindow.webContents.send('engine:status', {
        state: 'starting',
        message: 'Initializing Standalone FFmpeg + BRAW Engine...'
      });
    }

    watcherProcess = spawn(pythonBin, args, {
      cwd: projectDir,
      env: env
    });

    watcherProcess.stdout.on('data', (data) => {
      const lines = data.toString().split('\n');
      for (const line of lines) {
        if (line.trim().length > 0) {
          parseStreamLine(line);
        }
      }
    });

    watcherProcess.stderr.on('data', (data) => {
      const lines = data.toString().split('\n');
      for (const line of lines) {
        if (line.trim().length > 0) {
          parseStreamLine(line);
        }
      }
    });

    watcherProcess.on('close', (code) => {
      watcherProcess = null;
      if (mainWindow) {
        mainWindow.webContents.send('engine:log', `[GUI] Process exited with code ${code}`);
        mainWindow.webContents.send('engine:status', {
          state: 'stopped',
          currentClip: null,
          message: 'Watcher service stopped.'
        });
        mainWindow.webContents.send('engine:folderCounts', scanAllFolderCounts(activeRootFolder));
      }
    });

    watcherProcess.on('error', (err) => {
      watcherProcess = null;
      if (mainWindow) {
        mainWindow.webContents.send('engine:log', `[ERROR] Process failed to spawn: ${err.message}`);
        mainWindow.webContents.send('engine:status', {
          state: 'error',
          currentClip: null,
          message: `Process error: ${err.message}`
        });
      }
    });

    return { success: true };
  } catch (err) {
    return { success: false, message: err.message };
  }
});

ipcMain.handle('engine:stopWatcher', async () => {
  if (!watcherProcess) {
    return { success: true };
  }
  if (mainWindow) {
    mainWindow.webContents.send('engine:log', '[GUI] Sending graceful SIGINT stop signal to transcoder...');
    mainWindow.webContents.send('engine:status', {
      state: 'stopping',
      message: 'Stopping engine gracefully...'
    });
  }
  stopWatcherProcess();
  return { success: true };
});

ipcMain.handle('system:openExternal', async (_event, url) => {
  if (url && (url.startsWith('https://') || url.startsWith('http://'))) {
    await shell.openExternal(url);
    return true;
  }
  return false;
});

// Camera Tool 1 IPC Handlers
ipcMain.handle('camera:getConfig', () => {
  return {
    camera_ip: currentConfig.camera_ip || '192.168.1.118',
    camera_ftp: currentConfig.camera_ftp || 'ftp://PYXIS-6K.local',
    camera_auto_transfer: !!currentConfig.camera_auto_transfer
  };
});

ipcMain.handle('camera:saveConfig', (_event, newCamConfig) => {
  currentConfig = { ...currentConfig, ...newCamConfig };
  latestCameraStatus.camera_ip = currentConfig.camera_ip;
  latestCameraStatus.camera_ftp = currentConfig.camera_ftp;
  saveUserConfig();

  if (cameraProcess) {
    sendCameraCommand({
      cmd: 'update_config',
      camera_ip: currentConfig.camera_ip,
      camera_ftp: currentConfig.camera_ftp,
      dest_dir: path.join(activeRootFolder, '00_IN_INGEST')
    });
  }
  return { success: true, config: currentConfig };
});

ipcMain.handle('camera:connect', async (_event, params) => {
  const newIp = params?.camera_ip ? params.camera_ip.trim() : currentConfig.camera_ip;
  const newFtp = params?.camera_ftp ? params.camera_ftp.trim() : currentConfig.camera_ftp;

  if (newIp) currentConfig.camera_ip = newIp;
  if (newFtp) currentConfig.camera_ftp = newFtp;
  latestCameraStatus.camera_ip = currentConfig.camera_ip;
  latestCameraStatus.camera_ftp = currentConfig.camera_ftp;
  saveUserConfig();

  if (mainWindow) {
    mainWindow.webContents.send('engine:log', `[CAMERA] Connecting to camera at ${currentConfig.camera_ip}...`);
    mainWindow.webContents.send('camera:status', {
      ...latestCameraStatus,
      connecting: true
    });
  }

  if (!cameraProcess) {
    startCameraService(false);
    await new Promise(r => setTimeout(r, 600));
  } else {
    sendCameraCommand({
      cmd: 'update_config',
      camera_ip: currentConfig.camera_ip,
      camera_ftp: currentConfig.camera_ftp,
      dest_dir: path.join(activeRootFolder, '00_IN_INGEST')
    });
  }

  sendCameraCommand({ cmd: 'test_connection', camera_ip: currentConfig.camera_ip, camera_ftp: currentConfig.camera_ftp });
  return { success: true, camera_ip: currentConfig.camera_ip, camera_ftp: currentConfig.camera_ftp };
});

ipcMain.handle('camera:testConnection', async (_event, params) => {
  const ip = params?.camera_ip || currentConfig.camera_ip || '192.168.1.118';
  const ftp = params?.camera_ftp || currentConfig.camera_ftp || 'ftp://PYXIS-6K.local';

  if (!cameraProcess) {
    startCameraService(false);
    await new Promise(r => setTimeout(r, 600));
  }
  sendCameraCommand({ cmd: 'test_connection', camera_ip: ip, camera_ftp: ftp });
  return { queued: true };
});

ipcMain.handle('camera:toggleAutoTransfer', async (_event, active, importToday = false) => {
  currentConfig.camera_auto_transfer = active;
  if (active && !cameraProcess) {
    startCameraService(true);
    return { success: true, active: true };
  } else if (cameraProcess) {
    sendCameraCommand({ cmd: 'toggle', active: active, import_today: importToday });
    return { success: true, active: active };
  }
  return { success: true, active: false };
});

ipcMain.handle('camera:importToday', async () => {
  if (!cameraProcess) {
    startCameraService(false);
    await new Promise(r => setTimeout(r, 600));
  }
  sendCameraCommand({ cmd: 'import_today' });
  return { success: true };
});

ipcMain.handle('camera:getStatus', () => {
  if (cameraProcess) {
    sendCameraCommand({ cmd: 'status' });
  } else if (currentConfig.camera_auto_transfer) {
    startCameraService(true);
  }
  return latestCameraStatus;
});

app.whenReady().then(() => {
  debugLog('STARTUP', '══ Electron app.whenReady() fired ══');
  try {
    createWindow();
    debugLog('STARTUP', '✅ createWindow() completed successfully');
  } catch (err) {
    debugLog('FATAL', `createWindow() threw: ${err.stack || err.message}`);
    dialog.showErrorBox('Black Magic Converter — Startup Error',
      `Failed to create window:\n\n${err.stack || err.message}\n\nLog file: ${LOG_FILE}`);
  }

  app.on('activate', () => {
    debugLog('LIFECYCLE', 'App activate event');
    if (BrowserWindow.getAllWindows().length === 0) {
      try {
        createWindow();
      } catch (err) {
        debugLog('FATAL', `createWindow() on activate threw: ${err.stack || err.message}`);
      }
    }
  });
});

app.on('window-all-closed', () => {
  debugLog('LIFECYCLE', 'All windows closed');
  stopWatcherProcess();
  stopCameraService();
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('before-quit', () => {
  debugLog('LIFECYCLE', 'App before-quit — cleaning up watcher and camera processes');
  stopWatcherProcess();
  stopCameraService();
});
