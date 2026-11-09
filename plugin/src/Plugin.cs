using System;
using System.Collections.Generic;
using System.IO;
using System.Net.Http;
using System.Reflection;
using System.Text;
using BepInEx;
using BepInEx.Configuration;
using UnityEngine;

namespace Valhallowed.Plugin
{
    // The VHD plugin, loaded into each game runtime by BepInEx. It binds no
    // Harmony patch, so a game patch that renames a method cannot break it at
    // load. Once the server's world has loaded, it writes the world's object
    // count and player count to the status file the runtime's supervisor reads:
    // that count is world truth. It also samples the world's players and sends
    // them to intake as telemetry (B2).
    [BepInPlugin(Guid, Name, Version)]
    public sealed class Plugin : BaseUnityPlugin
    {
        public const string Guid = "com.valhallowed.plugin";
        public const string Name = "Valhallowed";

        // BepInEx needs a constant version. Releases are tags promoted without a
        // rebuild (G12), so the plugin knows its commit, never its release.
        public const string Version = "0.0.0";

        private const float SampleSeconds = 2f;
        // A position is sent only after a player moves this far from the last one sent.
        private const float MoveMetres = 3f;
        // Several minutes of positions for a full world of ten, should the sender stall.
        private const int QueueCapacity = 2000;
        // Intake's own request timeout.
        private const int PostTimeoutSeconds = 5;

        private ConfigEntry<string> _statusPath;
        private ConfigEntry<int> _statusIntervalSeconds;
        private ConfigEntry<string> _intakeUrl;
        private bool _worldLoaded;
        private bool _statusFailing;
        private float _nextStatusAt;

        private EventQueue _events;
        private Sender _sender;
        private HttpClient _http;
        private float _nextSampleAt;
        // The connected players, by the connection's uid: a reconnect is a new
        // uid, so it is a leave and a join.
        private readonly Dictionary<long, Connected> _roster = new Dictionary<long, Connected>();
        private readonly HashSet<long> _seen = new HashSet<long>();
        private readonly List<long> _gone = new List<long>();

        private sealed class Connected
        {
            // Null when the peer's id breaks B2: nothing is sent for it.
            public string Id;
            public Vector3? Sent;
        }

        private void Awake()
        {
            // The supervisor generates this config from the runtime's generation.
            // contracts/plugin.config.schema.json states every key bound here.
            _statusPath = Config.Bind("Status", "Path", "",
                "Where to write the world status file the runtime's supervisor reads. Empty writes none.");
            _statusIntervalSeconds = Config.Bind("Status", "IntervalSeconds", 5,
                "How often to rewrite the world status file, in seconds.");
            _intakeUrl = Config.Bind("Intake", "Url", "",
                "Where to post telemetry batches (B2). Empty sends none.");

            string build = typeof(Plugin).Assembly
                .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion ?? "unknown";
            Logger.LogInfo($"build {build} loaded; intake at {(_intakeUrl.Value.Length > 0 ? _intakeUrl.Value : "none")}");
        }

        private void Update()
        {
            ZNet net = ZNet.instance;
            ZDOMan objects = ZDOMan.instance;
            if (net == null || objects == null || !net.IsServer())
            {
                return;
            }

            if (!_worldLoaded)
            {
                _worldLoaded = true;
                Logger.LogInfo($"world {net.GetWorldName()} loaded with {objects.NrOfObjects()} objects");
                StartTelemetry();
            }

            if (_sender != null && Time.unscaledTime >= _nextSampleAt)
            {
                _nextSampleAt = Time.unscaledTime + SampleSeconds;
                SamplePlayers(net, objects);
            }

            if (Time.unscaledTime < _nextStatusAt)
            {
                return;
            }
            _nextStatusAt = Time.unscaledTime + Math.Max(1, _statusIntervalSeconds.Value);
            WriteStatus(net.GetWorldName(), objects.NrOfObjects(), net.GetPeers().Count);
        }

        private void OnDestroy()
        {
            _sender?.Stop();
            _http?.Dispose();
        }

        // One load of the world is one session, started when the world loads.
        private void StartTelemetry()
        {
            string url = _intakeUrl.Value;
            if (string.IsNullOrEmpty(url))
            {
                return;
            }
            string session = System.Guid.NewGuid().ToString("D");
            _events = new EventQueue(QueueCapacity, Logger.LogWarning);
            _http = new HttpClient { Timeout = TimeSpan.FromSeconds(PostTimeoutSeconds) };
            _sender = new Sender(_events, session, Batch.SessionStart(DateTime.UtcNow), body => Post(url, body),
                Logger.LogWarning, () => DateTime.UtcNow);
            _sender.Start();
            Logger.LogInfo($"session {session} started");
        }

        // Runs on the sender's thread, never the game's.
        private string Post(string url, string body)
        {
            using (var content = new StringContent(body, Encoding.UTF8, "application/json"))
            using (HttpResponseMessage response = _http.PostAsync(url, content).GetAwaiter().GetResult())
            {
                return response.IsSuccessStatusCode ? null : $"intake answered {(int)response.StatusCode}";
            }
        }

        // Who is connected, and where, read from the server's own state each
        // sample, not from patched game methods. A peer is connected once
        // ZNet.RPC_PeerInfo has set its uid and name together (ZNetPeer.IsReady);
        // a connection shorter than one sample is not seen. A position is the
        // player's character object, which the server holds, not the reference
        // position the client reports in RPC_ServerSyncedPlayerData.
        private void SamplePlayers(ZNet net, ZDOMan objects)
        {
            DateTime now = DateTime.UtcNow;
            _seen.Clear();
            foreach (ZNetPeer peer in net.GetPeers())
            {
                if (!peer.IsReady())
                {
                    continue;
                }
                _seen.Add(peer.m_uid);
                if (!_roster.TryGetValue(peer.m_uid, out Connected player))
                {
                    player = Join(peer, now);
                    _roster.Add(peer.m_uid, player);
                }
                if (player.Id == null)
                {
                    continue;
                }

                ZDO character = objects.GetZDO(peer.m_characterID);
                if (character == null)
                {
                    continue;
                }
                Vector3 at = character.GetPosition();
                if (!Batch.IsCoordinate(at.x) || !Batch.IsCoordinate(at.y) || !Batch.IsCoordinate(at.z)
                    || (player.Sent.HasValue && Vector3.Distance(player.Sent.Value, at) < MoveMetres))
                {
                    continue;
                }
                player.Sent = at;
                _events.Add(Batch.Position(now, player.Id, at.x, at.y, at.z));
            }

            _gone.Clear();
            foreach (KeyValuePair<long, Connected> entry in _roster)
            {
                if (!_seen.Contains(entry.Key))
                {
                    _gone.Add(entry.Key);
                }
            }
            foreach (long uid in _gone)
            {
                string id = _roster[uid].Id;
                if (id != null)
                {
                    _events.Add(Batch.Leave(now, id));
                }
                _roster.Remove(uid);
            }
        }

        private Connected Join(ZNetPeer peer, DateTime now)
        {
            string id = Players.SteamId(peer.m_socket?.GetHostName());
            if (id == null)
            {
                Logger.LogWarning($"player {peer.m_playerName} has no platform id telemetry can carry; sending nothing for them");
                return new Connected();
            }
            // A name that cleans to nothing sends no join; intake still learns
            // the player from their positions.
            string name = Players.Name(peer.m_playerName);
            if (name != null)
            {
                _events.Add(Batch.Join(now, id, name));
            }
            return new Connected { Id = id };
        }

        private void WriteStatus(string world, int objectCount, int players)
        {
            string path = _statusPath.Value;
            if (string.IsNullOrEmpty(path))
            {
                return;
            }

            string json = $"{{\"world\":{Batch.Quote(world)},\"objects\":{objectCount},\"players\":{players}}}";
            string temporary = path + ".tmp";
            try
            {
                // Written whole, then moved into place, so the supervisor never
                // reads half a report.
                File.WriteAllText(temporary, json);
                if (File.Exists(path))
                {
                    File.Replace(temporary, path, null);
                }
                else
                {
                    File.Move(temporary, path);
                }
                _statusFailing = false;
            }
            catch (Exception error) when (error is IOException || error is UnauthorizedAccessException)
            {
                // An observation fails soft: the world keeps running, and the
                // failure is logged once, not on every interval.
                if (!_statusFailing)
                {
                    Logger.LogWarning($"cannot write the world status to {path}: {error.Message}");
                }
                _statusFailing = true;
            }
        }
    }
}
