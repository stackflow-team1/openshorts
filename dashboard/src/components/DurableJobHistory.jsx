import { useCallback, useEffect, useState } from 'react';
import { apiFetch } from '../lib/api';

function prettyStage(stage) {
  return (stage || 'queued').replaceAll('_', ' ');
}

export default function DurableJobHistory({ onOpen, onRetry }) {
  const [jobs, setJobs] = useState([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const response = await apiFetch('/api/jobs?limit=20');
      if (!response.ok) throw new Error(await response.text());
      const data = await response.json();
      setJobs(Array.isArray(data.jobs) ? data.jobs : []);
    } catch (e) {
      setError(e.message || 'Could not load saved jobs.');
    } finally {
      setLoading(false);
    }
  }, []);
  const retry = async (jobId) => {
    try {
      await onRetry(jobId);
    } catch (e) {
      setError(e.message || 'Could not retry this job.');
    }
  };

  useEffect(() => {
    refresh();
  }, [refresh]);

  if (loading && jobs.length === 0) return null;

  return (
    <section className="w-full max-w-xl mt-8 text-left" aria-label="Job history">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-medium text-ink lowercase">Recent jobs</h2>
        <button type="button" onClick={refresh} className="text-xs text-muted hover:text-ink">
          Refresh
        </button>
      </div>
      {error && <p role="status" className="text-xs text-danger mb-3">{error}</p>}
      {jobs.length === 0 ? (
        <p className="text-xs text-muted">No saved jobs yet.</p>
      ) : (
        <div className="space-y-2">
          {jobs.map((job) => {
            const progress = job.progress || {};
            const name = job.source_metadata?.name || 'Video job';
            return (
              <article key={job.job_id} className="rounded-card border border-rule bg-paper2 p-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm text-ink">{name}</p>
                    <p className="mt-1 text-xs text-muted">
                      {job.status} · {prettyStage(progress.current_stage || job.current_stage)}
                    </p>
                    <p className="text-xs text-muted">
                      {progress.completed_clips || 0} completed · {progress.pending_clips || 0} pending
                      {progress.failed_clips ? ` · ${progress.failed_clips} failed` : ''}
                    </p>
                    {(job.interrupted || progress.recovery_message) && (
                      <p className="mt-1 text-xs text-brass">
                        {progress.recovery_message || 'Interrupted by an environment restart; recovery is in progress.'}
                      </p>
                    )}
                  </div>
                  <div className="flex shrink-0 gap-2">
                    {job.retryable && (
                      <button
                        type="button"
                        onClick={() => retry(job.job_id)}
                        className="btn-quiet px-2.5 py-1 text-xs"
                      >
                        Retry
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() => onOpen(job.job_id)}
                      className="btn-quiet px-2.5 py-1 text-xs"
                    >
                      Open
                    </button>
                  </div>
                </div>
                <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-rule" aria-label={`${progress.percent || 0}% complete`}>
                  <div
                    className="h-full bg-brass transition-all"
                    style={{ width: `${Math.min(100, Math.max(0, progress.percent || 0))}%` }}
                  />
                </div>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
