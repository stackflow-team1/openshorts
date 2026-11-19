import { ApiError, apiJson, getToken } from './api';
import { getApiUrl } from '../config';

const CHUNKED_UPLOAD_THRESHOLD = 64 * 1024 * 1024;
const MAX_UPLOAD_BYTES = 4096 * 1024 * 1024;
const MAX_CHUNK_RETRIES = 4;

const uploadStorageKey = (file) => `os_resumable_upload:${encodeURIComponent(JSON.stringify({
  name: file.name,
  size: file.size,
  lastModified: file.lastModified,
}))}`;

export const hasSavedUpload = (file) => {
  try {
    return !!localStorage.getItem(uploadStorageKey(file));
  } catch {
    return false;
  }
};

const postJson = (path, body, options = {}) => apiJson(path, {
  ...options,
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

function sendChunk(url, blob, onProgress, signal) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const cleanup = () => signal?.removeEventListener('abort', abort);
    const abort = () => {
      xhr.abort();
      const error = new Error('Upload canceled');
      error.name = 'AbortError';
      reject(error);
    };
    xhr.open('PUT', url);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    const token = getToken();
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
    signal?.addEventListener('abort', abort, { once: true });
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded);
    };
    xhr.onload = () => {
      cleanup();
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve();
        return;
      }
      let detail = xhr.responseText || `Upload chunk failed (${xhr.status})`;
      try {
        const body = JSON.parse(xhr.responseText);
        detail = body.detail || detail;
      } catch { /* use the response text */ }
      const error = new Error(String(detail));
      error.status = xhr.status;
      reject(error);
    };
    xhr.onerror = () => {
      cleanup();
      reject(new Error('Network error while uploading a chunk'));
    };
    xhr.ontimeout = () => {
      cleanup();
      reject(new Error('Chunk upload timed out'));
    };
    xhr.onabort = () => cleanup();
    xhr.timeout = 120000;
    if (signal?.aborted) abort();
    else xhr.send(blob);
  });
}

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function waitWhilePaused(control, signal) {
  if (!control?.paused) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const resume = () => {
      cleanup();
      resolve();
    };
    const cancel = () => {
      cleanup();
      const error = new Error('Upload canceled');
      error.name = 'AbortError';
      reject(error);
    };
    const cleanup = () => {
      control.waiters.delete(resume);
      signal?.removeEventListener('abort', cancel);
    };
    control.waiters.add(resume);
    signal?.addEventListener('abort', cancel, { once: true });
    if (signal?.aborted) cancel();
  });
}

async function retryChunk(url, blob, onProgress, signal) {
  for (let attempt = 0; ; attempt += 1) {
    try {
      await sendChunk(url, blob, onProgress, signal);
      return;
    } catch (error) {
      if (signal?.aborted || error.name === 'AbortError') throw error;
      const transient = !error.status || error.status === 408 || error.status === 429
        || error.status >= 500;
      if (!transient || attempt >= MAX_CHUNK_RETRIES) throw error;
      await pause(500 * (2 ** attempt));
    }
  }
}

export async function uploadVideo(file, onProgress = () => {}, { signal, control } = {}) {
  if (file.size > MAX_UPLOAD_BYTES) {
    throw new Error('File exceeds the 4 GiB upload limit.');
  }
  if (file.size <= CHUNKED_UPLOAD_THRESHOLD) {
    return { type: 'file', payload: file };
  }

  const storageKey = uploadStorageKey(file);
  let uploadId = localStorage.getItem(storageKey);
  try {
    let session = null;
    if (uploadId) {
      try {
        session = await apiJson(`/api/uploads/${encodeURIComponent(uploadId)}/status`, { signal });
        if (session.size !== file.size) session = null;
      } catch (error) {
        if (!(error instanceof ApiError) || error.status !== 404) throw error;
      }
      if (!session) {
        localStorage.removeItem(storageKey);
        uploadId = null;
      }
    }

    if (!session) {
      session = await postJson('/api/uploads/init', {
        filename: file.name,
        size: file.size,
      }, { signal });
      uploadId = session.upload_id;
      localStorage.setItem(storageKey, uploadId);
    }

    if (!session.complete) {
      const received = new Set(session.received_chunks || []);
      let acknowledgedBytes = [...received].reduce((sum, index) => {
        return sum + Math.min(session.chunk_size, file.size - index * session.chunk_size);
      }, 0);
      const initiallyUploaded = acknowledgedBytes;
      const startedAt = performance.now();
      const reportProgress = (loaded, chunkIndex) => {
        const elapsedSeconds = Math.max(0.001, (performance.now() - startedAt) / 1000);
        const speedBps = Math.max(0, loaded - initiallyUploaded) / elapsedSeconds;
        onProgress({
          loaded,
          total: file.size,
          chunkIndex,
          totalChunks: session.total_chunks,
          speedBps,
          etaSeconds: speedBps > 0 ? Math.ceil((file.size - loaded) / speedBps) : null,
        });
      };
      reportProgress(acknowledgedBytes, Math.min(received.size + 1, session.total_chunks));
      for (let index = 0; index < session.total_chunks; index += 1) {
        if (received.has(index)) continue;
        await waitWhilePaused(control, signal);
        if (signal?.aborted) {
          const error = new Error('Upload canceled');
          error.name = 'AbortError';
          throw error;
        }
        const start = index * session.chunk_size;
        const end = Math.min(file.size, start + session.chunk_size);
        const chunk = file.slice(start, end);
        const chunkUrl = `/api/uploads/${encodeURIComponent(uploadId)}/chunks/${index}`;
        await retryChunk(getApiUrl(chunkUrl), chunk, (loaded) => {
          reportProgress(Math.min(file.size, acknowledgedBytes + loaded), index + 1);
        }, signal);
        acknowledgedBytes += chunk.size;
        reportProgress(acknowledgedBytes, index + 1);
      }
      await waitWhilePaused(control, signal);
      if (signal?.aborted) {
        const error = new Error('Upload canceled');
        error.name = 'AbortError';
        throw error;
      }
      session = await postJson(`/api/uploads/${encodeURIComponent(uploadId)}/complete`, {}, { signal });
    }
    return { type: 'resumable_upload', payload: uploadId, storageKey };
  } catch (error) {
    if (signal?.aborted && uploadId) {
      try {
        const response = await apiJson(`/api/uploads/${encodeURIComponent(uploadId)}`, {
          method: 'DELETE',
        });
        if (!response.deleted) throw new Error('Could not cancel the upload session.');
        localStorage.removeItem(storageKey);
      } catch (cancelError) {
        if (!(cancelError instanceof ApiError && cancelError.status === 404)) throw cancelError;
        localStorage.removeItem(storageKey);
      }
    }
    throw error;
  }
}
