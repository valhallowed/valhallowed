using System;
using System.Collections.Generic;
using System.IO;
using System.Net.Http;
using System.Diagnostics;
using System.Reflection;
using System.Text;
using System.Threading;
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
    // them to intake as telemetry (B2). It also writes the world's map export
    // (B5), on a background thread, when the world's seed, world generator
    // version or the raster's resolution differs from the export it finds.
    [BepInPlugin(Guid, Name, Version)]
    public sealed class Plugin : BaseUnityPlugin
    {
        public const string Guid = "com.valhallowed.plugin";
        public const string Name = "Valhallowed";

        // BepInEx needs a constant version. Releases are tags promoted without a
        // rebuild (G12), so the plugin knows its commit, never its release.
        public const string Version = "0.0.0";

        private const float SampleSeconds = 2f;
        // Several minutes of positions for a full world of ten, should the sender stall.
        private const int QueueCapacity = 2000;
        // Intake's own request timeout.
        private const int PostTimeoutSeconds = 5;
        // The map export covers the whole world to its edge, where the world
        // generator ends the sea.
        private const float MapHalfWidth = WorldGenerator.waterEdge;
        // About 1000 pixels across, which is 21 m a pixel over the world's 21 km:
        // about three pixels to a 64 m zone, finer than the base image a map
        // shows needs, and a 10 MB raster of a million samples that one
        // background thread takes. A finer raster grows with the square.
        private const int MapSize = 1000;

        private ConfigEntry<string> _statusPath;
        private ConfigEntry<int> _statusIntervalSeconds;
        private ConfigEntry<string> _intakeUrl;
        private ConfigEntry<string> _mapExportDirectory;
        private bool _worldLoaded;
        private bool _statusFailing;
        private float _nextStatusAt;
        // True once the map export is started, or needs none.
        private bool _mapExportHandled;
        private readonly CancellationTokenSource _mapExportCancel = new CancellationTokenSource();

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
            // Time.unscaledTime when Sent was sent.
            public float SentAt;
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
            _mapExportDirectory = Config.Bind("MapExport", "Directory", "",
                "Where to write the world's map export (B5). Empty writes none.");

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

            if (!_mapExportHandled)
            {
                StartMapExport();
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
            _mapExportCancel.Cancel();
        }

        // Starts the map export once the world generator is up, which is after
        // the world loads, and reads what identifies the world on the game's
        // thread; the sampling itself runs on a thread of its own.
        private void StartMapExport()
        {
            string directory = _mapExportDirectory.Value;
            if (string.IsNullOrEmpty(directory))
            {
                _mapExportHandled = true;
                return;
            }
            WorldGenerator generator = WorldGenerator.instance;
            ZoneSystem zones = ZoneSystem.instance;
            if (generator == null || zones == null)
            {
                return;
            }
            _mapExportHandled = true;

            World world = generator.m_world;
            var identity = new WorldIdentity(world.m_name, world.m_seedName, world.m_seed, world.m_worldGenVersion, zones.m_waterLevel);
            var thread = new Thread(() => ExportMap(directory, identity, generator))
            {
                Name = "valhallowed-map-export",
                IsBackground = true,
                Priority = System.Threading.ThreadPriority.BelowNormal,
            };
            thread.Start();
        }

        // Runs on the export's thread, never the game's. An observation fails
        // soft: whatever goes wrong is logged and the world runs on.
        private void ExportMap(string directory, WorldIdentity identity, WorldGenerator generator)
        {
            try
            {
                if (MapExport.IsCurrent(directory, identity, MapSize, MapHalfWidth))
                {
                    Logger.LogInfo($"map export in {directory} is current");
                    return;
                }
                Logger.LogInfo($"writing the map export to {directory}");
                var clock = Stopwatch.StartNew();
                ExportFiles files = MapExport.Build(identity, MapSize, MapHalfWidth, (x, z) => SampleWorld(generator, x, z), _mapExportCancel.Token);
                MapExport.Write(directory, files);
                Logger.LogInfo($"wrote the map export in {clock.Elapsed.TotalSeconds:F1} s");
            }
            catch (OperationCanceledException)
            {
                Logger.LogInfo("map export stopped");
            }
            catch (Exception error)
            {
                Logger.LogWarning($"cannot write the map export to {directory}: {error.Message}");
            }
        }

        // The world generator's own answer at a point: its biome, the height it
        // gives that biome, and its forest factor. These methods read the
        // generator's fixed state and use only managed maths, Unity's value types
        // and Mathf.PerlinNoise, which Unity marks thread safe, so they run off
        // the game's thread; the game's own threads call them too (the river
        // cache is behind a lock for that). A generator the game has since
        // replaced would sample another world, so the export stops.
        private static Sample SampleWorld(WorldGenerator generator, float x, float z)
        {
            if (WorldGenerator.instance != generator)
            {
                throw new OperationCanceledException();
            }
            Heightmap.Biome biome = generator.GetBiome(x, z);
            float height = generator.GetBiomeHeight(biome, x, z, out Color _);
            float forest = WorldGenerator.GetForestFactor(new Vector3(x, 0f, z));
            return new Sample((ushort)biome, height, forest);
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
            float time = Time.unscaledTime;
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
                    || !Players.PositionDue(player.Sent.HasValue,
                        player.Sent.HasValue ? Vector3.Distance(player.Sent.Value, at) : 0f,
                        time - player.SentAt, SampleSeconds))
                {
                    continue;
                }
                player.Sent = at;
                player.SentAt = time;
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
                // The name is the client's choice, so it is cleaned before it reaches the log.
                Logger.LogWarning($"player {Players.Name(peer.m_playerName) ?? "with no name"} has no platform id telemetry can carry; sending nothing for them");
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
