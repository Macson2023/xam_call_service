'use strict';

const TOKEN_KEY = 'xam.call.token';
const API_PREFIX = '/api/v1';
const WS_PATH = '/ws/calls';
const PING_INTERVAL = 10_000;
const PONG_TIMEOUT = 8_000;
const CONNECT_TIMEOUT = 10_000;
const STABLE_CONNECTION_MS = 5_000;
const MAX_RECONNECT_DELAY = 15_000;
const SILENT_SOCKET_MS = PING_INTERVAL + PONG_TIMEOUT;
const AVATAR_COLORS = ['#6d8cff', '#a06bff', '#35d6a4', '#f4b85e', '#ff647c', '#4fc3f7'];

const state = {
  token: null,
  me: null,
  ws: null,
  wsStatus: 'offline',
  wsAttempts: 0,
  wsManualClose: false,
  wsConnecting: false,
  reconnectTimer: null,
  pingTimer: null,
  stableTimer: null,
  lastServerMessageAt: 0,
  localCandidates: [],
  hasConnectedOnce: false,
  serverWatched: new Set(),
  watched: new Set(),
  searchResults: [],
  users: new Map(),
  searchTimer: null,
  searchSeq: 0,
  config: null,
  call: null,
  direction: null,
  peer: null,
  cancelRequested: false,
  pc: null,
  localStream: null,
  remoteStream: null,
  pendingCandidates: [],
  iceStats: null,
  iceRestarts: 0,
  iceFailureReported: false,
  callStartedAt: null,
  tickTimer: null,
  statsTimer: null,
  lastStats: null,
  quickStream: null,
  toastTimer: null,
  diagOpen: false,
};

const ERROR_TEXT = {
  invalid_json: 'Сервер получил некорректный JSON',
  invalid_event: 'Некорректное событие сигналинга',
  invalid_payload: 'Некорректные данные сигналинга',
  unknown_event: 'Неизвестное событие сигналинга',
  user_offline: 'Пользователь не в сети',
  self_call: 'Нельзя позвонить самому себе',
  caller_busy: 'Вы уже участвуете в звонке',
  callee_busy: 'Пользователь уже участвует в звонке',
  call_not_found: 'Звонок не найден',
  forbidden: 'Нет доступа к этому звонку',
  invalid_state: 'Действие недоступно в текущем состоянии звонка',
  rate_limited: 'Слишком много событий, попробуйте ещё раз',
  payload_too_large: 'Слишком большое сообщение',
  internal_error: 'Внутренняя ошибка сервиса звонков',
};

const END_REASON_TEXT = {
  hangup: 'Звонок завершён',
  timeout: 'Собеседник не ответил',
  rejected: 'Звонок отклонён',
  cancelled: 'Звонок отменён',
  disconnected: 'Соединение потеряно',
  max_duration: 'Достигнут лимит времени звонка',
  server_shutdown: 'Сервер завершил звонок',
};

const ABORT_ERRORS = new Set([
  'user_offline',
  'self_call',
  'caller_busy',
  'callee_busy',
  'call_not_found',
]);

class ApiError extends Error {
  constructor(status, detail) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
  }
}

function $(id) {
  return document.getElementById(id);
}

function pad(value) {
  return String(value).padStart(2, '0');
}

function shortId(id) {
  if (!id) {
    return '—';
  }
  const text = String(id);
  return text.length > 12 ? `${text.slice(0, 8)}…` : text;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function displayName(user) {
  if (!user) {
    return 'Пользователь';
  }
  return user.display_name || user.username || shortId(user.id);
}

function loginLabel(user) {
  if (user?.username) {
    return `@${user.username}`;
  }
  return user?.id ? shortId(user.id) : '';
}

function initialsFor(user) {
  const name = displayName(user);
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (!parts.length) {
    return '?';
  }
  if (parts.length === 1) {
    return parts[0].slice(0, 2).toUpperCase();
  }
  return `${parts[0][0]}${parts[1][0]}`.toUpperCase();
}

function colorFor(seed) {
  const text = String(seed || '');
  let hash = 0;
  for (let index = 0; index < text.length; index += 1) {
    hash = (hash * 31 + text.charCodeAt(index)) >>> 0;
  }
  return AVATAR_COLORS[hash % AVATAR_COLORS.length];
}

function setAvatar(element, user) {
  if (!element) {
    return;
  }
  element.textContent = initialsFor(user);
  element.style.setProperty('--avatar', colorFor(user?.id || user?.username || 'x'));
}

function formatClock(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours > 0) {
    return `${pad(hours)}:${pad(minutes)}:${pad(seconds)}`;
  }
  return `${pad(minutes)}:${pad(seconds)}`;
}

function formatDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return '';
  }
  const today = new Date();
  const sameDay =
    date.getFullYear() === today.getFullYear() &&
    date.getMonth() === today.getMonth() &&
    date.getDate() === today.getDate();
  if (sameDay) {
    return date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }
  return date.toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function mediaLabel(media) {
  return media === 'video' ? 'Видеозвонок' : 'Аудиозвонок';
}

function statusLabel(status) {
  switch (status) {
    case 'completed':
      return 'Завершён';
    case 'missed':
      return 'Пропущен';
    case 'rejected':
      return 'Отклонён';
    case 'cancelled':
      return 'Отменён';
    case 'failed':
      return 'Ошибка';
    case 'active':
      return 'Идёт';
    case 'ringing':
      return 'Звонит';
    default:
      return status || '—';
  }
}

function statusClass(status) {
  switch (status) {
    case 'completed':
    case 'active':
      return 'status-text--ok';
    case 'missed':
    case 'rejected':
    case 'failed':
      return 'status-text--bad';
    case 'cancelled':
    case 'ringing':
      return 'status-text--warn';
    default:
      return 'status-text--muted';
  }
}

function toast(message, kind = 'info') {
  const element = $('toast');
  if (!element) {
    return;
  }
  element.textContent = message;
  element.classList.toggle('toast--error', kind === 'error');
  element.classList.toggle('toast--ok', kind === 'ok');
  element.hidden = false;
  if (state.toastTimer) {
    clearTimeout(state.toastTimer);
  }
  state.toastTimer = setTimeout(() => {
    element.hidden = true;
    state.toastTimer = null;
  }, 4600);
}

function setNotice(message) {
  const element = $('loginNotice');
  if (!message) {
    element.hidden = true;
    element.textContent = '';
    return;
  }
  element.textContent = message;
  element.hidden = false;
}

async function api(path, options = {}) {
  const headers = new Headers();
  if (options.body !== undefined) {
    headers.set('Content-Type', 'application/json');
  }
  if (state.token) {
    headers.set('Authorization', `Bearer ${state.token}`);
  }

  let response;
  try {
    response = await fetch(`${API_PREFIX}${path}`, {
      method: options.method || 'GET',
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw error;
    }
    throw new ApiError(0, 'Не удалось связаться с сервисом звонков');
  }

  if (response.status === 204) {
    return null;
  }

  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail =
      payload && typeof payload === 'object' && typeof payload.detail === 'string'
        ? payload.detail
        : `Ошибка запроса (${response.status})`;
    throw new ApiError(response.status, detail);
  }
  return payload;
}

function showLogin(message) {
  $('appScreen').hidden = true;
  $('loginScreen').hidden = false;
  setNotice(message || '');
}

function setMe(user) {
  state.me = user;
  if (!user) {
    return;
  }
  setAvatar($('meAvatar'), user);
  $('meName').textContent = displayName(user);
  $('meLogin').textContent = loginLabel(user);
}

function showApp() {
  $('loginScreen').hidden = true;
  $('appScreen').hidden = false;
  setNotice('');
  setMe(state.me);
  state.wsManualClose = false;
  state.wsAttempts = 0;
  state.hasConnectedOnce = false;
  state.serverWatched = new Set();
}

function applyAuthResponse(response) {
  state.token = response.access_token;
  localStorage.setItem(TOKEN_KEY, state.token);
  setMe(response.user);
  showApp();
  void loadConfig();
  connectWs();
  void loadHistory();
}

async function startSession(token) {
  state.token = token;
  state.wsManualClose = true;
  detachSocket();
  state.wsManualClose = false;
  setNotice('');
  try {
    const me = await api('/users/me');
    setMe(me);
    localStorage.setItem(TOKEN_KEY, token);
    showApp();
    void loadConfig();
    connectWs();
    void loadHistory();
  } catch (error) {
    localStorage.removeItem(TOKEN_KEY);
    state.token = null;
    state.me = null;
    showLogin(
      error instanceof ApiError && error.status === 401
        ? 'Сессия истекла. Войдите снова.'
        : error.message || 'Не удалось подключиться к сервису звонков.',
    );
  }
}

function clearSession(message) {
  state.wsManualClose = true;
  detachSocket();
  stopQuickPreview();
  cleanupCall(null, { refresh: false });
  state.token = null;
  state.me = null;
  state.config = null;
  state.users.clear();
  state.searchResults = [];
  state.serverWatched = new Set();
  state.watched = new Set();
  state.hasConnectedOnce = false;
  state.wsAttempts = 0;
  state.localCandidates = [];
  localStorage.removeItem(TOKEN_KEY);
  $('searchInput').value = '';
  $('searchResults').innerHTML = '<p class="empty">Введите username, чтобы найти собеседника.</p>';
  $('historyList').innerHTML = '<p class="empty">Пока нет звонков.</p>';
  showLogin(message);
}

function logout(message) {
  clearSession(message || 'Вы вышли из аккаунта.');
}

function detachSocket() {
  if (state.reconnectTimer) {
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  }
  stopHeartbeat();
  const socket = state.ws;
  state.ws = null;
  state.serverWatched = new Set();
  if (socket) {
    socket.onopen = null;
    socket.onmessage = null;
    socket.onclose = null;
    socket.onerror = null;
    try {
      socket.close(1000, 'client disconnect');
    } catch {
    }
  }
  setWsStatus('offline');
}

function wsUrl() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}${WS_PATH}`;
}

function timeoutSignal(ms) {
  if (typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function') {
    return AbortSignal.timeout(ms);
  }
  if (typeof AbortController !== 'undefined') {
    const controller = new AbortController();
    setTimeout(() => controller.abort(), ms);
    return controller.signal;
  }
  return undefined;
}

function setWsStatus(status) {
  state.wsStatus = status;
  const element = $('wsStatus');
  element.classList.remove(
    'status-pill--online',
    'status-pill--offline',
    'status-pill--connecting',
    'status-pill--reconnecting',
  );
  const text = {
    online: 'На связи',
    connecting: 'Подключение…',
    reconnecting: 'Переподключение…',
    offline: 'Нет связи',
  };
  element.classList.add(`status-pill--${status}`);
  element.textContent = text[status] || status;
  updateDiag();
}

function socketIsSilent(socket) {
  return Boolean(
    socket &&
      socket.readyState === WebSocket.OPEN &&
      state.lastServerMessageAt > 0 &&
      Date.now() - state.lastServerMessageAt > SILENT_SOCKET_MS,
  );
}

function stopHeartbeat() {
  if (state.pingTimer) {
    clearInterval(state.pingTimer);
    state.pingTimer = null;
  }
  if (state.stableTimer) {
    clearTimeout(state.stableTimer);
    state.stableTimer = null;
  }
  state.lastServerMessageAt = 0;
}

function startHeartbeat(socket) {
  stopHeartbeat();
  state.lastServerMessageAt = Date.now();
  state.pingTimer = setInterval(() => {
    if (state.ws !== socket || socket.readyState !== WebSocket.OPEN) {
      return;
    }
    if (socketIsSilent(socket)) {
      logEvent('!', 'heartbeat-timeout', {
        silent_ms: Date.now() - state.lastServerMessageAt,
      });
      dropSocket(socket, 'heartbeat timeout');
      return;
    }
    sendRaw({ type: 'ping' }, false);
  }, PING_INTERVAL);
}

function scheduleStableReset(socket) {
  if (state.stableTimer) {
    clearTimeout(state.stableTimer);
  }
  state.stableTimer = setTimeout(() => {
    state.stableTimer = null;
    if (state.ws === socket && socket.readyState === WebSocket.OPEN) {
      state.wsAttempts = 0;
    }
  }, STABLE_CONNECTION_MS);
}

function dropSocket(socket, reason) {
  if (state.ws !== socket) {
    try {
      socket.close();
    } catch {
    }
    return;
  }
  state.ws = null;
  state.serverWatched = new Set();
  stopHeartbeat();
  setWsStatus('reconnecting');
  try {
    socket.close(4000, reason);
  } catch {
  }
  scheduleReconnect();
}

function scheduleReconnect() {
  if (!state.token || state.wsManualClose || state.reconnectTimer) {
    return;
  }
  if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  const exponent = Math.min(state.wsAttempts, 6);
  const base = Math.min(1000 * 2 ** exponent, MAX_RECONNECT_DELAY);
  const delay = Math.round(base * (0.7 + Math.random() * 0.6));
  state.wsAttempts += 1;
  setWsStatus('reconnecting');
  state.reconnectTimer = setTimeout(() => {
    state.reconnectTimer = null;
    void connectWs();
  }, delay);
}

function reconnectNow(reason, force = false) {
  if (!state.token || state.wsManualClose) {
    return;
  }
  const socket = state.ws;
  if (socket && socket.readyState === WebSocket.OPEN) {
    if (!force && !socketIsSilent(socket)) {
      return;
    }
    state.ws = null;
    state.serverWatched = new Set();
    stopHeartbeat();
    try {
      socket.close(4000, reason);
    } catch {
    }
  } else if (socket && socket.readyState === WebSocket.CONNECTING) {
    return;
  } else {
    state.ws = null;
  }

  if (state.reconnectTimer) {
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = null;
  }
  state.wsAttempts = 0;
  void connectWs();
}

async function connectWs() {
  if (!state.token || state.wsManualClose) {
    return;
  }
  const current = state.ws;
  if (
    current &&
    (current.readyState === WebSocket.OPEN || current.readyState === WebSocket.CONNECTING)
  ) {
    return;
  }
  if (state.wsConnecting) {
    return;
  }

  state.wsConnecting = true;
  const sessionToken = state.token;
  setWsStatus(state.wsAttempts > 0 ? 'reconnecting' : 'connecting');

  try {
    let ticket;
    try {
      const response = await api('/calls/ws-ticket', {
        method: 'POST',
        signal: timeoutSignal(CONNECT_TIMEOUT),
      });
      ticket = response.ticket;
    } catch (error) {
      if (sessionToken !== state.token || state.wsManualClose) {
        return;
      }
      if (error instanceof ApiError && error.status === 401) {
        clearSession('Сессия истекла. Войдите снова.');
        return;
      }
      setWsStatus('offline');
      scheduleReconnect();
      return;
    }

    if (sessionToken !== state.token || state.wsManualClose) {
      return;
    }

    let socket;
    try {
      socket = new WebSocket(`${wsUrl()}?ticket=${encodeURIComponent(ticket)}`);
    } catch {
      setWsStatus('offline');
      scheduleReconnect();
      return;
    }

    state.ws = socket;

    socket.onopen = () => {
      if (state.ws !== socket) {
        try {
          socket.close();
        } catch {
        }
        return;
      }
      state.serverWatched = new Set();
      setWsStatus('online');
      startHeartbeat(socket);
      syncWatched(true);
    };

    socket.onmessage = (message) => {
      if (state.ws === socket) {
        state.lastServerMessageAt = Date.now();
      }
      if (typeof message.data !== 'string') {
        return;
      }
      let event;
      try {
        event = JSON.parse(message.data);
      } catch {
        logEvent('←', 'parse-error', { detail: message.data.slice(0, 80) });
        return;
      }
      if (event?.type === 'ready' && state.ws === socket) {
        scheduleStableReset(socket);
      }
      handleWsEvent(event).catch((error) => {
        console.error(error);
        toast('Ошибка обработки события звонка', 'error');
        logEvent('!', 'handler-error', { detail: String(error?.message || error) });
      });
    };

    socket.onclose = (event) => {
      if (state.ws !== socket) {
        return;
      }
      state.ws = null;
      state.serverWatched = new Set();
      stopHeartbeat();
      if (state.wsManualClose || !state.token) {
        setWsStatus('offline');
        return;
      }
      if (event.code === 1008) {
        clearSession('Сессия истекла. Войдите снова.');
        return;
      }
      setWsStatus('reconnecting');
      scheduleReconnect();
    };

    socket.onerror = () => {
      dropSocket(socket, 'socket error');
    };
  } finally {
    state.wsConnecting = false;
  }
}

function sendRaw(payload, writeLog = true) {
  const socket = state.ws;
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    return false;
  }
  try {
    socket.send(JSON.stringify(payload));
    if (writeLog) {
      logEvent('→', payload.type, payload.data);
    }
    return true;
  } catch {
    return false;
  }
}

function sendSignal(type, callId, extra) {
  if (!callId) {
    return false;
  }
  return sendRaw({ type, data: { call_id: callId, ...extra } });
}

function desiredWatchIds() {
  const ids = new Set();
  state.searchResults.forEach((user) => {
    if (user?.id) {
      ids.add(user.id);
    }
  });
  if (state.peer?.id) {
    ids.add(state.peer.id);
  }
  if (state.me?.id) {
    ids.delete(state.me.id);
  }
  return ids;
}

function syncWatched(force = false) {
  const desired = desiredWatchIds();
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    state.watched = desired;
    return;
  }
  const previous = force ? new Set() : state.serverWatched;
  const add = [...desired].filter((id) => !previous.has(id));
  const remove = [...previous].filter((id) => !desired.has(id));
  if (add.length) {
    sendRaw({ type: 'presence.watch', data: { user_ids: add } }, false);
  }
  if (remove.length) {
    sendRaw({ type: 'presence.unwatch', data: { user_ids: remove } }, false);
  }
  state.serverWatched = desired;
  state.watched = desired;
}

function applyPresence(data) {
  if (!data?.user_id) {
    return;
  }
  const existing = state.users.get(data.user_id) || { id: data.user_id };
  const user = {
    ...existing,
    online: Boolean(data.online),
    in_call: Boolean(data.in_call),
  };
  state.users.set(data.user_id, user);

  state.searchResults = state.searchResults.map((item) =>
    item.id === data.user_id ? { ...item, online: user.online, in_call: user.in_call } : item,
  );
  if (state.peer?.id === data.user_id) {
    state.peer = { ...state.peer, online: user.online, in_call: user.in_call };
    updateCallUi();
  }
  renderSearchResults();
  updateDiag();
}

function handleReady(data) {
  if (data.user) {
    const merged = {
      ...(state.me || {}),
      ...data.user,
      username: data.user.username || state.me?.username || null,
      display_name: data.user.display_name || state.me?.display_name || null,
    };
    setMe(merged);
  }
  if (data.ring_timeout_seconds !== undefined) {
    state.config = {
      ...(state.config || {}),
      ring_timeout_seconds: data.ring_timeout_seconds,
      disconnect_grace_seconds: data.disconnect_grace_seconds,
      media: data.media,
    };
  }

  const serverCall = data.call || null;
  if (serverCall) {
    const direction = serverCall.caller.id === state.me?.id ? 'outgoing' : 'incoming';
    const peer = direction === 'outgoing' ? serverCall.callee : serverCall.caller;

    if (!state.hasConnectedOnce && !state.pc) {
      endStaleCall(serverCall, direction);
    } else if (serverCall.state === 'active') {
      if (!state.pc) {
        endStaleCall(serverCall, direction);
      } else {
        const resumed = state.hasConnectedOnce;
        state.call = serverCall;
        state.direction = direction;
        state.peer = peer;
        showCallStage();
        updateCallUi();
        startCallTimer(serverCall.answered_at);
        if (direction === 'outgoing') {
          void maybeCreateOffer(serverCall);
        }
        if (resumed) {
          resendLocalSignaling(serverCall);
        }
      }
    } else if (serverCall.state === 'ringing') {
      state.call = serverCall;
      state.direction = direction;
      state.peer = peer;
      if (direction === 'incoming') {
        showIncoming(serverCall);
      } else if (state.localStream) {
        showCallStage();
        updateCallUi();
      } else {
        endStaleCall(serverCall, direction);
      }
    }
  } else if (state.call?.id) {
    cleanupCall('Звонок завершён', { refresh: true });
  }

  state.hasConnectedOnce = true;
  syncWatched(true);
  updateDiag();
}

function endStaleCall(call, direction) {
  if (direction === 'outgoing' && call.state === 'ringing') {
    sendRaw({ type: 'call.cancel', data: { call_id: call.id } });
  } else if (direction === 'incoming' && call.state === 'ringing') {
    sendRaw({ type: 'call.reject', data: { call_id: call.id } });
  } else {
    sendRaw({ type: 'call.hangup', data: { call_id: call.id } });
  }
  cleanupCall('Предыдущая сессия звонка завершена', { refresh: true });
}

function handleServerError(data) {
  const code = data?.code || 'error';
  const message = ERROR_TEXT[code] || data?.detail || 'Ошибка сигналинга';
  toast(message, 'error');
  logEvent('!', code, data);
  if (state.call && state.call.state === 'preparing') {
    cleanupCall(null, { refresh: false });
    return;
  }
  if (
    ABORT_ERRORS.has(code) &&
    state.call &&
    (state.call.state === 'preparing' || state.call.state === 'ringing')
  ) {
    cleanupCall(null, { refresh: false });
  }
}

async function handleWsEvent(event) {
  if (!event || typeof event.type !== 'string') {
    return;
  }
  logEvent('←', event.type, event.data);

  switch (event.type) {
    case 'ready':
      handleReady(event.data || {});
      return;

    case 'pong':
      return;

    case 'presence.state':
      applyPresence(event.data);
      return;

    case 'call.ringing': {
      const call = event.data?.call;
      if (!call) {
        return;
      }
      if (state.cancelRequested) {
        sendRaw({ type: 'call.cancel', data: { call_id: call.id } });
        state.cancelRequested = false;
        cleanupCall('Звонок отменён', { refresh: true });
        return;
      }
      state.call = call;
      state.direction = 'outgoing';
      state.peer = call.callee;
      showCallStage();
      updateCallUi();
      syncWatched();
      return;
    }

    case 'call.incoming': {
      const call = event.data?.call;
      if (!call) {
        return;
      }
      if (state.call && state.call.id !== call.id) {
        logEvent('!', 'incoming-while-busy', { call_id: call.id });
        sendRaw({ type: 'call.reject', data: { call_id: call.id } });
        return;
      }
      state.call = call;
      state.direction = 'incoming';
      state.peer = call.caller;
      showIncoming(call);
      syncWatched();
      return;
    }

    case 'call.accepted': {
      const call = event.data?.call;
      if (!call) {
        return;
      }
      state.call = call;
      state.direction = call.caller.id === state.me?.id ? 'outgoing' : 'incoming';
      state.peer = state.direction === 'outgoing' ? call.callee : call.caller;
      hideIncoming();
      if (!state.pc) {
        await createPeer(call);
      }
      showCallStage();
      updateCallUi();
      startCallTimer(call.answered_at);
      if (state.direction === 'outgoing') {
        await maybeCreateOffer(call);
      }
      syncWatched();
      return;
    }

    case 'call.rejected':
      if (state.call) {
        cleanupCall('Собеседник отклонил звонок', { refresh: true });
      } else {
        void loadHistory();
      }
      return;

    case 'call.cancelled':
      if (state.call) {
        cleanupCall('Звонок отменён', { refresh: true });
      } else {
        void loadHistory();
      }
      return;

    case 'call.ended': {
      const call = event.data?.call;
      const reason = call?.end_reason;
      if (state.call) {
        cleanupCall(END_REASON_TEXT[reason] || 'Звонок завершён', { refresh: true });
      } else {
        void loadHistory();
      }
      return;
    }

    case 'webrtc.offer': {
      const data = event.data || {};
      if (!state.call || data.call_id !== state.call.id) {
        return;
      }
      await handleOffer(data);
      return;
    }

    case 'webrtc.answer': {
      const data = event.data || {};
      if (!state.call || data.call_id !== state.call.id) {
        return;
      }
      await handleAnswer(data);
      return;
    }

    case 'webrtc.ice': {
      const data = event.data || {};
      if (!state.call || data.call_id !== state.call.id) {
        return;
      }
      await handleRemoteIce(data);
      return;
    }

    case 'error':
      handleServerError(event.data || {});
      return;

    default:
      logEvent('!', 'unknown-event', { type: event.type });
  }
}

function setCallStatus(text) {
  $('callStatus').textContent = text;
}

function updateCallUi() {
  const call = state.call;
  if (!call) {
    return;
  }
  const peer = state.peer || (state.direction === 'outgoing' ? call.callee : call.caller);
  const peerName = displayName(peer);
  $('callPeer').textContent = peerName;
  $('remoteLabel').textContent = peerName;
  setAvatar($('remoteAvatar'), peer);
  setAvatar($('localAvatar'), state.me);

  const grid = $('videoGrid');
  grid.classList.toggle('audio-only', call.media === 'audio');
  $('cameraBtn').hidden = call.media !== 'video';

  const callState = call.state || 'preparing';
  if (callState === 'preparing') {
    setCallStatus('Готовим звонок…');
  } else if (callState === 'ringing') {
    setCallStatus(state.direction === 'incoming' ? 'Входящий звонок' : 'Звоним собеседнику…');
  } else if (callState === 'active') {
    const connection = state.pc?.connectionState;
    if (connection === 'connected') {
      setCallStatus('В эфире');
    } else if (connection === 'failed') {
      setCallStatus('WebRTC не установил соединение');
    } else {
      setCallStatus('Соединяем…');
    }
  }

  updateLocalPlaceholder();
  updateRemotePlaceholder();
  updateMediaButtons();
  updateDiag();
}

function updateLocalPlaceholder() {
  const track = state.localStream?.getVideoTracks?.()[0];
  const show = !track || !track.enabled;
  $('localPlaceholder').hidden = !show;
  if (state.localStream) {
    const video = $('localVideo');
    if (video.srcObject !== state.localStream) {
      video.srcObject = state.localStream;
    }
    void video.play().catch(() => {});
  }
}

function updateRemotePlaceholder() {
  const track = state.remoteStream?.getVideoTracks?.()[0];
  $('remotePlaceholder').hidden = Boolean(track);
}

function updateMediaButtons() {
  const audioTrack = state.localStream?.getAudioTracks?.()[0];
  const videoTrack = state.localStream?.getVideoTracks?.()[0];
  const mute = $('muteBtn');
  mute.dataset.on = String(audioTrack ? audioTrack.enabled : false);
  mute.textContent = audioTrack && audioTrack.enabled ? 'Микрофон' : 'Микрофон выкл.';

  const camera = $('cameraBtn');
  camera.disabled = !videoTrack;
  camera.dataset.on = String(videoTrack ? videoTrack.enabled : false);
  camera.textContent = videoTrack && videoTrack.enabled ? 'Камера' : 'Камера выкл.';
}

function showCallStage() {
  $('previewState').hidden = true;
  $('callStage').hidden = false;
  updateCallUi();
}

function resetCallView() {
  $('callStage').hidden = true;
  $('previewState').hidden = false;
  $('incomingModal').hidden = true;
  $('callTimer').textContent = '00:00';
  $('callStats').textContent = 'WebRTC: ожидание подключения';
  $('videoGrid').classList.remove('audio-only');
  $('remoteVideo').srcObject = null;
  $('localVideo').srcObject = null;
  $('remotePlaceholder').hidden = false;
  $('localPlaceholder').hidden = false;
  $('cameraBtn').hidden = false;
  $('cameraBtn').disabled = false;
  $('muteBtn').dataset.on = 'true';
  $('muteBtn').textContent = 'Микрофон';
}

function showIncoming(call) {
  if (!call) {
    return;
  }
  const peer = call.caller;
  setAvatar($('incomingAvatar'), peer);
  $('incomingName').textContent = displayName(peer);
  $('incomingMeta').textContent = `${mediaLabel(call.media)} · ${loginLabel(peer)}`;
  $('acceptBtn').disabled = false;
  $('rejectBtn').disabled = false;
  $('incomingModal').hidden = false;
  setCallStatus('Входящий звонок');
  updateDiag();
}

function hideIncoming() {
  $('incomingModal').hidden = true;
  $('acceptBtn').disabled = false;
  $('rejectBtn').disabled = false;
}

async function acceptIncoming() {
  const call = state.call;
  if (!call || state.direction !== 'incoming' || call.state !== 'ringing') {
    return;
  }
  $('acceptBtn').disabled = true;
  $('rejectBtn').disabled = true;
  await ensureIceConfig();
  if (!state.token) {
    hideIncoming();
    return;
  }
  try {
    await ensureLocalMedia(call.media);
  } catch (error) {
    toast(mediaErrorText(error), 'error');
    sendRaw({ type: 'call.reject', data: { call_id: call.id } });
    hideIncoming();
    cleanupCall(null, { refresh: false });
    return;
  }
  if (state.call !== call) {
    stopLocalStream();
    return;
  }
  try {
    await createPeer(call);
  } catch (error) {
    toast(`Не удалось создать WebRTC: ${error.message}`, 'error');
    sendRaw({ type: 'call.reject', data: { call_id: call.id } });
    hideIncoming();
    cleanupCall(null, { refresh: false });
    return;
  }
  if (!sendRaw({ type: 'call.accept', data: { call_id: call.id } })) {
    toast('Нет соединения с сервером сигналинга', 'error');
    hideIncoming();
    cleanupCall(null, { refresh: false });
    return;
  }
  hideIncoming();
  showCallStage();
  updateCallUi();
}

function rejectIncoming() {
  const call = state.call;
  if (!call || state.direction !== 'incoming') {
    hideIncoming();
    return;
  }
  sendRaw({ type: 'call.reject', data: { call_id: call.id } });
  hideIncoming();
  cleanupCall('Звонок отклонён', { refresh: true });
}

async function startCall(user, media) {
  if (!user?.id) {
    toast('Пользователь не найден', 'error');
    return;
  }
  if (state.call) {
    toast('Уже есть активный звонок', 'error');
    return;
  }
  if (state.me?.id === user.id) {
    toast('Нельзя позвонить самому себе', 'error');
    return;
  }
  if (user.online === false) {
    toast('Пользователь не в сети', 'error');
    return;
  }
  if (user.in_call) {
    toast('Пользователь уже участвует в звонке', 'error');
    return;
  }
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    toast('Нет соединения с сервером сигналинга', 'error');
    return;
  }

  stopQuickPreview();
  await ensureIceConfig();
  if (!state.token) {
    return;
  }
  try {
    await ensureLocalMedia(media);
  } catch (error) {
    toast(mediaErrorText(error), 'error');
    return;
  }

  state.call = {
    id: null,
    media,
    caller: state.me,
    callee: user,
    state: 'preparing',
    status: 'preparing',
  };
  state.direction = 'outgoing';
  state.peer = user;
  state.cancelRequested = false;
  showCallStage();
  updateCallUi();
  syncWatched();

  const sent = sendRaw({
    type: 'call.invite',
    data: { to_user_id: user.id, media },
  });
  if (!sent) {
    cleanupCall('Не удалось отправить приглашение', { refresh: false });
  }
}

async function startCallById(media) {
  const input = $('userIdInput');
  const id = input.value.trim();
  if (!id) {
    toast('Введите UUID пользователя', 'error');
    return;
  }
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)) {
    toast('UUID имеет неверный формат', 'error');
    return;
  }
  await startCall({ id, online: undefined, in_call: false }, media);
}

function endCall() {
  const call = state.call;
  if (!call) {
    cleanupCall(null, { refresh: false });
    return;
  }
  if (!call.id) {
    cleanupCall('Звонок отменён', { refresh: false });
    state.cancelRequested = true;
    return;
  }
  if (call.state === 'ringing') {
    sendRaw({
      type: state.direction === 'outgoing' ? 'call.cancel' : 'call.reject',
      data: { call_id: call.id },
    });
  } else {
    sendRaw({ type: 'call.hangup', data: { call_id: call.id } });
  }
  cleanupCall('Звонок завершён', { refresh: true });
}

function cleanupCall(message, options = {}) {
  const hadCall = Boolean(state.call);
  hideIncoming();
  stopCallTimer();
  stopStats();
  closePeer();
  stopLocalStream();
  state.call = null;
  state.direction = null;
  state.peer = null;
  state.cancelRequested = false;
  resetCallView();
  if (hadCall) {
    syncWatched();
  }
  if (message) {
    toast(message);
  }
  if (options.refresh) {
    setTimeout(() => {
      if (state.token) {
        void loadHistory();
      }
    }, 260);
  }
  updateDiag();
}

async function ensureLocalMedia(media) {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error('WebRTC недоступен. Откройте клиент на localhost или по HTTPS.');
  }
  stopLocalStream();
  const constraints = {
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
    video:
      media === 'video'
        ? {
            width: { ideal: 1280 },
            height: { ideal: 720 },
            frameRate: { ideal: 30, max: 30 },
            facingMode: 'user',
          }
        : false,
  };
  const stream = await navigator.mediaDevices.getUserMedia(constraints);
  state.localStream = stream;
  const video = $('localVideo');
  video.srcObject = stream;
  video.muted = true;
  void video.play().catch(() => {});
  updateLocalPlaceholder();
  updateMediaButtons();
  updateDiag();
  return stream;
}

function stopLocalStream() {
  if (state.localStream) {
    state.localStream.getTracks().forEach((track) => track.stop());
    state.localStream = null;
  }
  const video = $('localVideo');
  if (video) {
    video.srcObject = null;
  }
  $('localPlaceholder').hidden = false;
  updateMediaButtons();
  updateDiag();
}

async function ensureIceConfig() {
  if (!state.config) {
    await loadConfig();
  }
}

function iceServerUrls() {
  const servers = state.config?.ice_servers;
  if (!Array.isArray(servers)) {
    return [];
  }
  return servers.flatMap((server) => {
    if (!server) {
      return [];
    }
    return Array.isArray(server.urls) ? server.urls : [server.urls];
  }).filter(Boolean).map(String);
}

function hasTurnServer() {
  return iceServerUrls().some((url) => url.startsWith('turn:') || url.startsWith('turns:'));
}

function iceServersSummary() {
  const urls = iceServerUrls();
  if (!urls.length) {
    return 'не загружены (используется STUN по умолчанию)';
  }
  const stun = urls.filter((url) => url.startsWith('stun:') || url.startsWith('stuns:')).length;
  const turn = urls.filter((url) => url.startsWith('turn:') || url.startsWith('turns:')).length;
  return `STUN ${stun} · TURN ${turn}${turn ? '' : ' · TURN не настроен'}`;
}

function candidateType(candidate) {
  const match = / typ (\w+)/.exec(candidate?.candidate || '');
  return match ? match[1] : 'unknown';
}

function resetIceStats() {
  state.iceStats = { host: 0, srflx: 0, relay: 0, errors: [] };
  state.iceRestarts = 0;
  state.iceFailureReported = false;
}

async function maybeRestartIce() {
  const pc = state.pc;
  const call = state.call;
  if (!pc || !call?.id || call.state !== 'active' || state.direction !== 'outgoing') {
    return;
  }
  if (state.iceRestarts >= 2 || pc.signalingState !== 'stable') {
    return;
  }
  state.iceRestarts += 1;
  state.localCandidates = [];
  logEvent('!', 'ice-restart', { attempt: state.iceRestarts });
  try {
    if (typeof pc.restartIce === 'function') {
      pc.restartIce();
    }
    const offer = await pc.createOffer({ iceRestart: true });
    await pc.setLocalDescription(offer);
    sendSignal('webrtc.offer', call.id, { sdp: pc.localDescription.sdp });
  } catch (error) {
    logEvent('!', 'ice-restart-error', { detail: String(error?.message || error) });
  }
}

function reportIceFailure(reason) {
  const detail = `${reason || 'connection failed'} · ${iceServersSummary()} · host ${
    state.iceStats?.host || 0
  } srflx ${state.iceStats?.srflx || 0} relay ${state.iceStats?.relay || 0}`;
  const firstFailure = !state.iceFailureReported;
  state.iceFailureReported = true;
  logEvent('!', firstFailure ? 'ice-failed' : 'ice-failed-again', { detail });
  if (firstFailure) {
    toast(
      hasTurnServer()
        ? 'WebRTC не установил соединение. Проверьте доступность TURN (порты 3478 и 49160-49200) и STUN.'
        : 'WebRTC не установил соединение: TURN не настроен. Задайте TURN_URLS/TURN_USERNAME/TURN_CREDENTIAL.',
      'error',
    );
  }
  void maybeRestartIce();
}

async function createPeer(call) {
  if (state.pc) {
    return state.pc;
  }
  const iceServers =
    state.config?.ice_servers && state.config.ice_servers.length
      ? state.config.ice_servers
      : [{ urls: ['stun:stun.l.google.com:19302'] }];
  logEvent('·', 'ice-config', { detail: iceServersSummary() });

  const pc = new RTCPeerConnection({
    iceServers,
    bundlePolicy: 'max-bundle',
    rtcpMuxPolicy: 'require',
  });
  state.pc = pc;
  state.localCandidates = [];
  resetIceStats();

  if (state.localStream) {
    state.localStream.getTracks().forEach((track) => {
      pc.addTrack(track, state.localStream);
    });
  }

  pc.onicecandidate = (event) => {
    if (!event.candidate) {
      return;
    }
    const candidate = event.candidate.toJSON();
    const type = candidateType(candidate);
    if (type in state.iceStats) {
      state.iceStats[type] += 1;
      if (state.iceStats[type] === 1) {
        logEvent('·', `ice-${type}`, { candidate });
      }
    }
    state.localCandidates.push(candidate);
    if (state.localCandidates.length > 300) {
      state.localCandidates.shift();
    }
    if (state.call?.id) {
      sendSignal('webrtc.ice', state.call.id, { candidate });
    }
  };

  pc.onicecandidateerror = (event) => {
    if (!state.iceStats) {
      return;
    }
    const detail = `${event.address || '?'}:${event.port || '?'} ${
      event.errorCode || ''
    } ${event.errorText || ''}`.trim();
    if (state.iceStats.errors.length < 5) {
      state.iceStats.errors.push(detail);
    }
    logEvent('!', 'ice-candidate-error', { detail });
  };

  pc.ontrack = (event) => {
    const stream = event.streams[0] || new MediaStream([event.track]);
    state.remoteStream = stream;
    const video = $('remoteVideo');
    if (video.srcObject !== stream) {
      video.srcObject = stream;
    }
    void video.play().catch(() => {});
    updateRemotePlaceholder();
    updateDiag();
  };

  pc.onconnectionstatechange = () => {
    if (pc.connectionState === 'failed') {
      reportIceFailure('connectionState=failed');
    }
    updateCallUi();
  };

  pc.oniceconnectionstatechange = () => {
    if (pc.iceConnectionState === 'failed') {
      reportIceFailure('iceConnectionState=failed');
    }
    updateDiag();
  };
  pc.onicegatheringstatechange = () => updateDiag();
  pc.onsignalingstatechange = () => updateDiag();

  startStats();
  updateDiag();
  return pc;
}

async function maybeCreateOffer(call) {
  const pc = state.pc;
  if (!pc || !call || state.direction !== 'outgoing') {
    return;
  }
  if (pc.signalingState !== 'stable' || pc.localDescription) {
    return;
  }
  const offer = await pc.createOffer({
    offerToReceiveAudio: true,
    offerToReceiveVideo: call.media === 'video',
  });
  await pc.setLocalDescription(offer);
  sendSignal('webrtc.offer', call.id, { sdp: pc.localDescription.sdp });
}

function resendLocalSignaling(call) {
  const pc = state.pc;
  if (!pc || !call?.id) {
    return;
  }
  try {
    if (pc.localDescription?.sdp) {
      const type = pc.localDescription.type === 'offer' ? 'webrtc.offer' : 'webrtc.answer';
      sendSignal(type, call.id, { sdp: pc.localDescription.sdp });
    }
    state.localCandidates.forEach((candidate) => {
      sendSignal('webrtc.ice', call.id, { candidate });
    });
  } catch (error) {
    logEvent('!', 'resend-error', { detail: String(error?.message || error) });
  }
}

async function handleOffer(data) {
  const call = state.call;
  if (!call || data.call_id !== call.id) {
    return;
  }
  const pc = state.pc || (await createPeer(call));
  if (pc.signalingState !== 'stable') {
    logEvent('!', 'offer-ignored', { state: pc.signalingState });
    return;
  }
  await pc.setRemoteDescription({ type: 'offer', sdp: data.sdp });
  await flushCandidates();
  const answer = await pc.createAnswer();
  await pc.setLocalDescription(answer);
  sendSignal('webrtc.answer', call.id, { sdp: pc.localDescription.sdp });
}

async function handleAnswer(data) {
  const pc = state.pc;
  if (!pc) {
    return;
  }
  if (pc.signalingState !== 'have-local-offer') {
    logEvent('!', 'answer-ignored', { state: pc.signalingState });
    return;
  }
  await pc.setRemoteDescription({ type: 'answer', sdp: data.sdp });
  await flushCandidates();
}

async function handleRemoteIce(data) {
  if (!data.candidate) {
    return;
  }
  const pc = state.pc;
  if (!pc || !pc.remoteDescription) {
    state.pendingCandidates.push(data.candidate);
    return;
  }
  try {
    await pc.addIceCandidate(data.candidate);
  } catch (error) {
    logEvent('!', 'ice-error', { detail: String(error?.message || error) });
  }
}

async function flushCandidates() {
  const pc = state.pc;
  if (!pc) {
    return;
  }
  const candidates = state.pendingCandidates.splice(0);
  for (const candidate of candidates) {
    try {
      await pc.addIceCandidate(candidate);
    } catch (error) {
      logEvent('!', 'ice-error', { detail: String(error?.message || error) });
    }
  }
}

function closePeer() {
  stopStats();
  const pc = state.pc;
  state.pc = null;
  state.pendingCandidates = [];
  state.localCandidates = [];
  if (pc) {
    pc.onicecandidate = null;
    pc.ontrack = null;
    pc.onconnectionstatechange = null;
    pc.oniceconnectionstatechange = null;
    pc.onicegatheringstatechange = null;
    pc.onsignalingstatechange = null;
    try {
      pc.close();
    } catch {
    }
  }
  state.remoteStream = null;
  $('remoteVideo').srcObject = null;
  $('remotePlaceholder').hidden = false;
  updateDiag();
}

function startCallTimer(answeredAt) {
  stopCallTimer();
  const parsed = answeredAt ? Date.parse(answeredAt) : Date.now();
  state.callStartedAt = Number.isFinite(parsed) ? parsed : Date.now();
  const tick = () => {
    $('callTimer').textContent = formatClock(Date.now() - state.callStartedAt);
  };
  tick();
  state.tickTimer = setInterval(tick, 1000);
}

function stopCallTimer() {
  if (state.tickTimer) {
    clearInterval(state.tickTimer);
    state.tickTimer = null;
  }
  state.callStartedAt = null;
  $('callTimer').textContent = '00:00';
}

function startStats() {
  stopStats();
  state.lastStats = null;
  state.statsTimer = setInterval(() => {
    void pollStats();
  }, 2000);
  void pollStats();
}

function stopStats() {
  if (state.statsTimer) {
    clearInterval(state.statsTimer);
    state.statsTimer = null;
  }
  state.lastStats = null;
}

async function pollStats() {
  const pc = state.pc;
  if (!pc) {
    return;
  }
  let reports;
  try {
    reports = await pc.getStats();
  } catch {
    return;
  }

  let inboundVideo = null;
  let outboundVideo = null;
  let inboundAudio = null;
  let outboundAudio = null;
  reports.forEach((report) => {
    if (report.type === 'inbound-rtp') {
      if (report.kind === 'video' || report.mediaType === 'video') {
        inboundVideo = report;
      } else if (report.kind === 'audio' || report.mediaType === 'audio') {
        inboundAudio = report;
      }
    } else if (report.type === 'outbound-rtp') {
      if (report.kind === 'video' || report.mediaType === 'video') {
        outboundVideo = report;
      } else if (report.kind === 'audio' || report.mediaType === 'audio') {
        outboundAudio = report;
      }
    }
  });

  const now = performance.now();
  const previous = state.lastStats;
  const inBytes =
    (inboundVideo?.bytesReceived || 0) + (inboundAudio?.bytesReceived || 0);
  const outBytes =
    (outboundVideo?.bytesSent || 0) + (outboundAudio?.bytesSent || 0);

  let inRate = 0;
  let outRate = 0;
  if (previous) {
    const seconds = Math.max(0.001, (now - previous.now) / 1000);
    inRate = Math.max(0, (inBytes - previous.inBytes) * 8) / seconds / 1_000_000;
    outRate = Math.max(0, (outBytes - previous.outBytes) * 8) / seconds / 1_000_000;
  }
  state.lastStats = { now, inBytes, outBytes };

  const parts = [];
  if (inboundVideo) {
    const width = inboundVideo.frameWidth || 0;
    const height = inboundVideo.frameHeight || 0;
    const fps = Math.round(inboundVideo.framesPerSecond || 0);
    if (width && height) {
      parts.push(`Видео ${width}×${height}${fps ? ` @ ${fps} fps` : ''}`);
    } else {
      parts.push('Видео');
    }
  } else if (inboundAudio) {
    parts.push('Аудио');
  } else {
    parts.push('WebRTC');
  }
  parts.push(`${inRate.toFixed(2)} Мбит/с ↓`);
  parts.push(`${outRate.toFixed(2)} Мбит/с ↑`);

  $('callStats').textContent = parts.join(' · ');
  updateDiag();
}

async function startQuickPreview() {
  if (state.call) {
    toast('Сначала завершите звонок', 'error');
    return;
  }
  stopQuickPreview();
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: true,
      video: {
        width: { ideal: 1280 },
        height: { ideal: 720 },
        frameRate: { ideal: 30, max: 30 },
        facingMode: 'user',
      },
    });
    state.quickStream = stream;
    const video = $('quickPreview');
    video.srcObject = stream;
    video.muted = true;
    void video.play().catch(() => {});
    $('quickPreviewBox').hidden = false;
    toast('Камера и микрофон работают', 'ok');
  } catch (error) {
    toast(mediaErrorText(error), 'error');
  }
}

function stopQuickPreview() {
  if (state.quickStream) {
    state.quickStream.getTracks().forEach((track) => track.stop());
    state.quickStream = null;
  }
  const video = $('quickPreview');
  if (video) {
    video.srcObject = null;
  }
  $('quickPreviewBox').hidden = true;
}

function mediaErrorText(error) {
  if (!navigator.mediaDevices?.getUserMedia) {
    return 'WebRTC недоступен: откройте клиент на localhost или по HTTPS.';
  }
  if (error?.message && /^[А-Яа-яЁё]/.test(error.message)) {
    return error.message;
  }
  switch (error?.name) {
    case 'NotAllowedError':
      return 'Доступ к камере и микрофону запрещён. Разрешите доступ в браузере.';
    case 'NotFoundError':
      return 'Камера или микрофон не найдены.';
    case 'NotReadableError':
      return 'Камера или микрофон заняты другим приложением.';
    case 'OverconstrainedError':
      return 'Устройство не поддерживает запрошенное качество видео.';
    case 'SecurityError':
      return 'Браузер заблокировал доступ к медиа. Нужен localhost или HTTPS.';
    default:
      return 'Не удалось получить доступ к камере или микрофону.';
  }
}

function toggleMute() {
  const track = state.localStream?.getAudioTracks?.()[0];
  if (!track) {
    toast('Микрофон не подключён', 'error');
    return;
  }
  track.enabled = !track.enabled;
  updateMediaButtons();
  updateDiag();
}

function toggleCamera() {
  const track = state.localStream?.getVideoTracks?.()[0];
  if (!track) {
    toast('Камера не подключена', 'error');
    return;
  }
  track.enabled = !track.enabled;
  updateLocalPlaceholder();
  updateMediaButtons();
  updateDiag();
}

async function loadConfig() {
  try {
    state.config = await api('/calls/config');
    updateDiag();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      clearSession('Сессия истекла. Войдите снова.');
      return;
    }
    logEvent('!', 'config-error', { detail: error.message });
  }
}

async function loadHistory(manual = false) {
  if (!state.token) {
    return;
  }
  try {
    const page = await api('/calls/history?limit=30');
    renderHistory(page.items || []);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      clearSession('Сессия истекла. Войдите снова.');
      return;
    }
    if (manual) {
      toast(error.message || 'Не удалось загрузить историю', 'error');
    }
  }
}

function renderHistory(items) {
  const container = $('historyList');
  if (!items.length) {
    container.innerHTML = '<p class="empty">Пока нет звонков.</p>';
    return;
  }
  container.innerHTML = items
    .map((item) => {
      const peer = item.peer || {};
      const peerName = displayName(peer);
      const direction = item.direction === 'outgoing' ? 'Исходящий' : 'Входящий';
      const duration = item.duration_seconds ? ` · ${formatClock(item.duration_seconds * 1000)}` : '';
      const icon = item.media === 'video' ? 'V' : 'A';
      const title = `${direction} · ${mediaLabel(item.media)} · ${statusLabel(item.status)}`;
      return `
        <div class="history-row" title="${escapeHtml(title)}">
          <div class="history-row__icon">${icon}</div>
          <div class="history-row__meta">
            <strong>${escapeHtml(peerName)}</strong>
            <span>${direction} · ${formatDateTime(item.created_at)}${duration}</span>
          </div>
          <div class="history-row__status ${statusClass(item.status)}">${statusLabel(
            item.status,
          )}</div>
        </div>`;
    })
    .join('');
}

function renderSearchResults() {
  const container = $('searchResults');
  if (!state.searchResults.length) {
    container.innerHTML = '<p class="empty">Ничего не найдено.</p>';
    return;
  }
  container.innerHTML = state.searchResults
    .map((user) => {
      const online = Boolean(user.online);
      const inCall = Boolean(user.in_call);
      const isMe = user.id === state.me?.id;
      const disabled = !online || inCall || isMe;
      const presence = online ? 'в сети' : 'не в сети';
      const badge = inCall ? '<span class="badge">в звонке</span>' : '';
      return `
        <article class="user-row" data-user-id="${escapeHtml(user.id)}">
          <div class="avatar avatar--sm" style="--avatar:${colorFor(user.id)}">${escapeHtml(
            initialsFor(user),
          )}</div>
          <div class="user-row__meta">
            <strong>${escapeHtml(displayName(user))}</strong>
            <span>${escapeHtml(loginLabel(user))}</span>
          </div>
          <span class="user-row__presence ${online ? 'is-online' : ''}">${presence}</span>
          ${badge}
          <div class="user-row__actions">
            <button class="button" type="button" data-call-media="audio" ${
              disabled ? 'disabled' : ''
            }>Аудио</button>
            <button class="button" type="button" data-call-media="video" ${
              disabled ? 'disabled' : ''
            }>Видео</button>
          </div>
        </article>`;
    })
    .join('');
}

async function runSearch(query) {
  const text = query.trim();
  const sequence = ++state.searchSeq;
  if (!text) {
    state.searchResults = [];
    renderSearchResults();
    syncWatched();
    return;
  }
  try {
    const users = await api(`/users/search?q=${encodeURIComponent(text)}&limit=20`);
    if (sequence !== state.searchSeq) {
      return;
    }
    state.searchResults = users;
    users.forEach((user) => {
      state.users.set(user.id, { ...state.users.get(user.id), ...user });
    });
    renderSearchResults();
    syncWatched();
  } catch (error) {
    if (sequence !== state.searchSeq) {
      return;
    }
    if (error instanceof ApiError && error.status === 401) {
      clearSession('Сессия истекла. Войдите снова.');
      return;
    }
    $('searchResults').innerHTML = `<p class="empty">${escapeHtml(
      error.message || 'Поиск недоступен',
    )}</p>`;
  }
}

function logEvent(direction, type, data) {
  if (type === 'pong' || type === 'presence.watch' || type === 'presence.unwatch') {
    return;
  }
  let summary = '';
  if (data?.call) {
    summary = `${shortId(data.call.id)} ${data.call.state} ${data.call.media}`;
  } else if (data?.call_id) {
    const parts = [shortId(data.call_id)];
    if (typeof data.sdp === 'string') {
      parts.push(`sdp=${data.sdp.length}`);
    }
    if ('candidate' in data) {
      const candidate = data.candidate?.candidate || '';
      parts.push(candidate ? `ice=${candidate.slice(0, 46)}` : 'ice=end');
    }
    summary = parts.join(' ');
  } else if (data?.user_id) {
    summary = `user=${shortId(data.user_id)} online=${Boolean(data.online)} in_call=${Boolean(
      data.in_call,
    )}`;
  } else if (data?.detail) {
    summary = `${data.code || ''} ${data.detail}`.trim();
  } else if (data?.type) {
    summary = data.type;
  }
  const line = `${new Date().toLocaleTimeString('ru-RU')} ${direction} ${type}${
    summary ? ` — ${summary}` : ''
  }`;
  const element = $('eventLog');
  const next = `${element.textContent}${element.textContent ? '\n' : ''}${line}`;
  element.textContent = next.length > 14_000 ? next.slice(-14_000) : next;
  element.scrollTop = element.scrollHeight;
}

function updateDiag() {
  const element = $('diagStatus');
  if (!element) {
    return;
  }
  const localTracks = state.localStream
    ? state.localStream.getTracks().map((track) => track.kind).join(' + ')
    : '—';
  const remoteTracks = state.remoteStream
    ? state.remoteStream.getTracks().map((track) => track.kind).join(' + ')
    : '—';
  const call = state.call;
  const items = [
    ['WebSocket', state.wsStatus],
    ['Звонок', call ? `${shortId(call.id)} · ${call.state || 'preparing'} · ${call.media}` : '—'],
    ['Собеседник', state.peer ? `${displayName(state.peer)} · ${shortId(state.peer.id)}` : '—'],
    ['ICE-серверы', iceServersSummary()],
    [
      'ICE-кандидаты',
      state.iceStats
        ? `host ${state.iceStats.host} · srflx ${state.iceStats.srflx} · relay ${
            state.iceStats.relay
          }${state.iceStats.errors.length ? ` · ошибок ${state.iceStats.errors.length}` : ''}`
        : '—',
    ],
    ['ICE connection', state.pc?.iceConnectionState || '—'],
    ['Peer connection', state.pc?.connectionState || '—'],
    ['Signaling', state.pc?.signalingState || '—'],
    ['Локальные треки', localTracks],
    ['Удалённые треки', remoteTracks],
    [
      'Тайминги',
      state.config
        ? `ring ${state.config.ring_timeout_seconds ?? '—'}с · grace ${
            state.config.disconnect_grace_seconds ?? '—'
          }с`
        : '—',
    ],
  ];
  element.innerHTML = items
    .map(
      ([label, value]) =>
        `<div class="diag-item"><span>${escapeHtml(label)}</span><span>${escapeHtml(
          value,
        )}</span></div>`,
    )
    .join('');
}

function bindAuth() {
  document.querySelectorAll('[data-auth-tab]').forEach((button) => {
    button.addEventListener('click', () => {
      const tab = button.dataset.authTab;
      document.querySelectorAll('[data-auth-tab]').forEach((item) => {
        item.classList.toggle('is-active', item === button);
      });
      $('loginForm').hidden = tab !== 'login';
      $('registerForm').hidden = tab !== 'register';
      setNotice('');
    });
  });

  $('loginForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = $('loginSubmit');
    const username = $('loginUsername').value.trim();
    const password = $('loginPassword').value;
    if (!username || !password) {
      setNotice('Введите логин и пароль.');
      return;
    }
    button.disabled = true;
    button.textContent = 'Вход…';
    setNotice('');
    try {
      const response = await api('/auth/login', {
        method: 'POST',
        body: { username, password },
      });
      applyAuthResponse(response);
    } catch (error) {
      setNotice(error.message || 'Не удалось войти');
    } finally {
      button.disabled = false;
      button.textContent = 'Войти';
    }
  });

  $('registerForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = $('registerSubmit');
    const username = $('registerUsername').value.trim();
    const displayNameValue = $('registerDisplayName').value.trim();
    const password = $('registerPassword').value;
    if (!username || !displayNameValue || !password) {
      setNotice('Заполните все поля.');
      return;
    }
    button.disabled = true;
    button.textContent = 'Создаём…';
    setNotice('');
    try {
      const response = await api('/auth/register', {
        method: 'POST',
        body: {
          username,
          password,
          display_name: displayNameValue,
        },
      });
      applyAuthResponse(response);
    } catch (error) {
      setNotice(error.message || 'Не удалось зарегистрироваться');
    } finally {
      button.disabled = false;
      button.textContent = 'Создать аккаунт';
    }
  });

  $('tokenForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const token = $('tokenInput').value.trim();
    if (!token) {
      setNotice('Вставьте access token.');
      return;
    }
    setNotice('');
    await startSession(token);
  });
}

function bindApp() {
  $('logoutBtn').addEventListener('click', () => logout());

  $('searchForm').addEventListener('submit', (event) => {
    event.preventDefault();
    void runSearch($('searchInput').value);
  });

  $('searchInput').addEventListener('input', () => {
    if (state.searchTimer) {
      clearTimeout(state.searchTimer);
    }
    state.searchTimer = setTimeout(() => {
      state.searchTimer = null;
      void runSearch($('searchInput').value);
    }, 350);
  });

  $('refreshUsers').addEventListener('click', () => {
    void runSearch($('searchInput').value);
  });

  $('searchResults').addEventListener('click', (event) => {
    const button = event.target.closest('[data-call-media]');
    if (!button) {
      return;
    }
    const row = button.closest('[data-user-id]');
    if (!row) {
      return;
    }
    const user = state.users.get(row.dataset.userId);
    if (user) {
      void startCall(user, button.dataset.callMedia);
    }
  });

  document.querySelectorAll('[data-id-call]').forEach((button) => {
    button.addEventListener('click', () => {
      void startCallById(button.dataset.idCall);
    });
  });

  $('refreshHistory').addEventListener('click', () => {
    void loadHistory(true);
  });

  $('localPreviewBtn').addEventListener('click', () => {
    void startQuickPreview();
  });

  $('stopPreview').addEventListener('click', () => {
    stopQuickPreview();
  });

  $('diagBtn').addEventListener('click', () => {
    state.diagOpen = !state.diagOpen;
    $('diagPanel').hidden = !state.diagOpen;
    updateDiag();
  });

  $('clearLog').addEventListener('click', () => {
    $('eventLog').textContent = '';
  });

  $('muteBtn').addEventListener('click', toggleMute);
  $('cameraBtn').addEventListener('click', toggleCamera);
  $('hangupBtn').addEventListener('click', endCall);
  $('acceptBtn').addEventListener('click', () => {
    void acceptIncoming();
  });
  $('rejectBtn').addEventListener('click', rejectIncoming);

  window.addEventListener('online', () => {
    reconnectNow('network online', true);
  });

  window.addEventListener('offline', () => {
    if (!state.ws) {
      setWsStatus('offline');
    }
  });

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      reconnectNow('page visible');
    }
  });

  window.addEventListener('pagehide', () => {
    const call = state.call;
    if (call?.id && state.ws?.readyState === WebSocket.OPEN) {
      try {
        state.ws.send(
          JSON.stringify({ type: 'call.hangup', data: { call_id: call.id } }),
        );
      } catch {
        }
    }
  });
}

function init() {
  bindAuth();
  bindApp();

  const hash = window.location.hash.replace(/^#/, '');
  const hashParams = new URLSearchParams(hash);
  const hashToken = hashParams.get('token') || hashParams.get('access_token');
  if (hashToken) {
    localStorage.setItem(TOKEN_KEY, hashToken);
    history.replaceState(null, '', `${window.location.pathname}${window.location.search}`);
  }

  const token = localStorage.getItem(TOKEN_KEY);
  if (token) {
    void startSession(token);
  } else {
    showLogin('');
  }
  updateDiag();
}

document.addEventListener('DOMContentLoaded', init);
