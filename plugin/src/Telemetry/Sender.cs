using System;
using System.Collections.Generic;
using System.Threading;

namespace Valhallowed.Plugin
{
    // Sends the queued events to intake as B2 batches, off the game thread.
    // A batch intake does not accept is dropped, never retried, with one
    // exception: session.start rides at the head of every batch until one is
    // accepted, because a world can load before intake answers and the
    // session would otherwise never start. Intake records only the first
    // start it takes, so carrying it again is harmless.
    internal sealed class Sender
    {
        public const int IntervalMs = 5000;
        private const int StopWaitMs = 2000;

        private readonly EventQueue _queue;
        private readonly string _session;
        // Posts one batch: null when intake accepted it, else why not.
        private readonly Func<string, string> _post;
        private readonly Action<string> _log;
        private readonly Func<DateTime> _now;
        private readonly ManualResetEvent _stop = new ManualResetEvent(false);
        private string _start;
        private long _failed;
        private Thread _thread;

        public Sender(EventQueue queue, string session, string start, Func<string, string> post,
            Action<string> log, Func<DateTime> now)
        {
            _queue = queue;
            _session = session;
            _start = start;
            _post = post;
            _log = log;
            _now = now;
        }

        public void Start()
        {
            _thread = new Thread(Run) { IsBackground = true, Name = "valhallowed-sender" };
            _thread.Start();
        }

        public void Stop()
        {
            _stop.Set();
            _thread?.Join(StopWaitMs);
        }

        private void Run()
        {
            while (!_stop.WaitOne(IntervalMs))
            {
                SendQueued();
            }
        }

        // Sends what is queued, a full batch at a time. A batch that fails
        // ends the round, so an unreachable intake costs one post per interval.
        public void SendQueued()
        {
            while (true)
            {
                int room = _start == null ? Batch.MaxEvents : Batch.MaxEvents - 1;
                List<string> events = _queue.Take(room);
                int taken = events.Count;
                if (_start != null)
                {
                    events.Insert(0, _start);
                }
                if (events.Count == 0)
                {
                    return;
                }

                string refused;
                try
                {
                    refused = _post(Batch.Envelope(_session, _now(), events));
                }
                catch (Exception error)
                {
                    refused = error.Message;
                }

                if (refused != null)
                {
                    if (_failed == 0)
                    {
                        _log($"cannot send telemetry to intake: {refused}; dropping batches until it accepts one");
                    }
                    _failed++;
                    return;
                }
                if (_failed > 0)
                {
                    _log($"intake accepts telemetry again; {_failed} batches dropped");
                    _failed = 0;
                }
                _start = null;
                if (taken < room)
                {
                    return;
                }
            }
        }
    }
}
