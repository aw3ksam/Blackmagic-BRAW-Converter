const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  // Directory & OS dialogs
  selectDirectory: () => ipcRenderer.invoke('dialog:selectDirectory'),
  revealInFinder: (subfolder) => ipcRenderer.invoke('dialog:revealInFinder', subfolder),

  // Engine state & control
  getInitialState: () => ipcRenderer.invoke('engine:getInitialState'),
  scanFolderCounts: () => ipcRenderer.invoke('engine:scanFolderCounts'),
  saveConfig: (config) => ipcRenderer.invoke('engine:saveConfig', config),
  startWatcher: () => ipcRenderer.invoke('engine:startWatcher'),
  stopWatcher: () => ipcRenderer.invoke('engine:stopWatcher'),
  openExternal: (url) => ipcRenderer.invoke('system:openExternal', url),

  // Event listeners
  onLog: (callback) => {
    const listener = (_event, logLine) => callback(logLine);
    ipcRenderer.on('engine:log', listener);
    return () => ipcRenderer.removeListener('engine:log', listener);
  },
  onStatus: (callback) => {
    const listener = (_event, status) => callback(status);
    ipcRenderer.on('engine:status', listener);
    return () => ipcRenderer.removeListener('engine:status', listener);
  },
  onProgress: (callback) => {
    const listener = (_event, data) => callback(data);
    ipcRenderer.on('engine:progress', listener);
    return () => ipcRenderer.removeListener('engine:progress', listener);
  },
  onSpeed: (callback) => {
    const listener = (_event, data) => callback(data);
    ipcRenderer.on('engine:speed', listener);
    return () => ipcRenderer.removeListener('engine:speed', listener);
  },
  onFolderCounts: (callback) => {
    const listener = (_event, counts) => callback(counts);
    ipcRenderer.on('engine:folderCounts', listener);
    return () => ipcRenderer.removeListener('engine:folderCounts', listener);
  },

  // Camera Tool 1 Auto Ingest API
  camera: {
    getConfig: () => ipcRenderer.invoke('camera:getConfig'),
    saveConfig: (cfg) => ipcRenderer.invoke('camera:saveConfig', cfg),
    testConnection: (params) => ipcRenderer.invoke('camera:testConnection', params),
    connect: (params) => ipcRenderer.invoke('camera:connect', params),
    toggleAutoTransfer: (active, importToday) => ipcRenderer.invoke('camera:toggleAutoTransfer', active, importToday),
    importToday: () => ipcRenderer.invoke('camera:importToday'),
    getStatus: () => ipcRenderer.invoke('camera:getStatus'),
    onStatus: (callback) => {
      const listener = (_event, data) => callback(data);
      ipcRenderer.on('camera:status', listener);
      return () => ipcRenderer.removeListener('camera:status', listener);
    },
    onProgress: (callback) => {
      const listener = (_event, data) => callback(data);
      ipcRenderer.on('camera:progress', listener);
      return () => ipcRenderer.removeListener('camera:progress', listener);
    },
    onCompleted: (callback) => {
      const listener = (_event, data) => callback(data);
      ipcRenderer.on('camera:completed', listener);
      return () => ipcRenderer.removeListener('camera:completed', listener);
    },
    onRecord: (callback) => {
      const listener = (_event, data) => callback(data);
      ipcRenderer.on('camera:record', listener);
      return () => ipcRenderer.removeListener('camera:record', listener);
    },
    onTestResult: (callback) => {
      const listener = (_event, data) => callback(data);
      ipcRenderer.on('camera:testResult', listener);
      return () => ipcRenderer.removeListener('camera:testResult', listener);
    }
  }
});
