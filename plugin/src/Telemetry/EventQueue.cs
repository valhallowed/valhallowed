using System;
using System.Collections.Generic;

namespace Valhallowed.Plugin
{
    // The events waiting for the sender. The game thread adds and the sender
    // takes; a full queue drops the event and counts it, so the game never
    // waits (B2 fails soft). A drop is logged once per spell of being full.
    internal sealed class EventQueue
    {
        private readonly object _lock = new object();
        private readonly Queue<string> _events = new Queue<string>();
        private readonly int _capacity;
        private readonly Action<string> _warn;
        private long _dropped;
        private bool _full;

        public EventQueue(int capacity, Action<string> warn)
        {
            _capacity = capacity;
            _warn = warn;
        }

        public long Dropped
        {
            get { lock (_lock) { return _dropped; } }
        }

        public void Add(string json)
        {
            long dropped;
            lock (_lock)
            {
                if (_events.Count < _capacity)
                {
                    _events.Enqueue(json);
                    return;
                }
                _dropped++;
                if (_full)
                {
                    return;
                }
                _full = true;
                dropped = _dropped;
            }
            _warn($"telemetry queue full at {_capacity} events; dropping events, {dropped} so far");
        }

        // Up to max events, oldest first.
        public List<string> Take(int max)
        {
            lock (_lock)
            {
                var taken = new List<string>(Math.Min(max, _events.Count));
                while (taken.Count < max && _events.Count > 0)
                {
                    taken.Add(_events.Dequeue());
                }
                if (taken.Count > 0)
                {
                    _full = false;
                }
                return taken;
            }
        }
    }
}
