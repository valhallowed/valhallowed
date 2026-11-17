using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Threading;

namespace Valhallowed.Plugin
{
    // One pixel of the world as the world generator samples it.
    internal readonly struct Sample
    {
        public readonly ushort Biome;
        public readonly float Height;
        public readonly float Forest;

        public Sample(ushort biome, float height, float forest)
        {
            Biome = biome;
            Height = height;
            Forest = forest;
        }
    }

    // The world generator at world-space (x, z), in metres.
    internal delegate Sample Sampler(float x, float z);

    // What identifies a world's terrain, which the game keeps.
    internal sealed class WorldIdentity
    {
        public readonly string Name;
        public readonly string SeedName;
        public readonly int Seed;
        public readonly int GeneratorVersion;
        public readonly float WaterLevel;

        public WorldIdentity(string name, string seedName, int seed, int generatorVersion, float waterLevel)
        {
            Name = name;
            SeedName = seedName;
            Seed = seed;
            GeneratorVersion = generatorVersion;
            WaterLevel = waterLevel;
        }
    }

    // The two files of a map export, as bytes.
    internal sealed class ExportFiles
    {
        public readonly byte[] Raster;
        public readonly byte[] Meta;

        public ExportFiles(byte[] raster, byte[] meta)
        {
            Raster = raster;
            Meta = meta;
        }
    }

    // B5, written by hand like B2 (the plugin may reference no JSON library).
    // contracts/plugin.map-export.schema.json is the contract, which states the
    // raster's layout, and the plugin's test holds this writer to its example.
    // Nothing here touches the game, so it runs off the game thread.
    internal static class MapExport
    {
        public const int Contract = 1;
        public const string RasterFile = "raster.bin";
        public const string MetaFile = "meta.json";
        // uint16 biome, float32 height and float32 forest factor.
        public const int BytesPerPixel = 10;
        // A bound under which size * size * BytesPerPixel stays below int.MaxValue, so the raster fits one array.
        public const int MaxSize = 14000;
        // A meta is under 400 bytes; the cap keeps a wrong file from being read whole.
        private const int MaxMetaBytes = 4096;

        // The contract's biome values: the game's Heightmap.Biome flags.
        private static readonly HashSet<ushort> Biomes = new HashSet<ushort> { 1, 2, 4, 8, 16, 32, 64, 256, 512 };

        // Samples size by size pixels over world space from -halfWidth to +halfWidth,
        // row 0 north. pause, when given, runs after each row, so a caller can
        // throttle the sweep.
        public static ExportFiles Build(WorldIdentity world, int size, float halfWidth, Sampler sample, CancellationToken cancel,
            Action pause = null)
        {
            Check(world, size, halfWidth);
            int pixels = size * size;
            var raster = new byte[pixels * BytesPerPixel];
            int heightPlane = pixels * 2;
            int forestPlane = pixels * 6;
            double pitch = 2.0 * halfWidth / size;
            for (int row = 0; row < size; row++)
            {
                cancel.ThrowIfCancellationRequested();
                float z = (float)(halfWidth - (row + 0.5) * pitch);
                for (int column = 0; column < size; column++)
                {
                    float x = (float)(-halfWidth + (column + 0.5) * pitch);
                    Sample at = sample(x, z);
                    if (!Biomes.Contains(at.Biome))
                    {
                        throw new InvalidOperationException($"the world generator gave biome {at.Biome} at ({x}, {z}), which B5 does not name");
                    }
                    if (!IsFinite(at.Height) || !IsFinite(at.Forest))
                    {
                        throw new InvalidOperationException($"the world generator gave a height or forest factor at ({x}, {z}) that is not finite");
                    }
                    int pixel = row * size + column;
                    raster[pixel * 2] = (byte)at.Biome;
                    raster[pixel * 2 + 1] = (byte)(at.Biome >> 8);
                    PutSingle(raster, heightPlane + pixel * 4, at.Height);
                    PutSingle(raster, forestPlane + pixel * 4, at.Forest);
                }
                pause?.Invoke();
            }

            string meta = Prefix(world, size, halfWidth) + raster.Length + ",\"sha256\":\"" + Hash(raster) + "\"}}\n";
            return new ExportFiles(raster, Encoding.UTF8.GetBytes(meta));
        }

        // Whether the directory already holds this world's export: a meta.json
        // that this writer wrote for the same world, seed, generator version and
        // geometry, and a raster that has the length and SHA-256 it names. Any
        // other state, a missing or unreadable file included, is not current.
        public static bool IsCurrent(string directory, WorldIdentity world, int size, float halfWidth)
        {
            Check(world, size, halfWidth);
            try
            {
                string metaPath = Path.Combine(directory, MetaFile);
                string rasterPath = Path.Combine(directory, RasterFile);
                if (!File.Exists(metaPath) || !File.Exists(rasterPath) || new FileInfo(metaPath).Length > MaxMetaBytes)
                {
                    return false;
                }

                string meta = File.ReadAllText(metaPath, Encoding.UTF8);
                string prefix = Prefix(world, size, halfWidth);
                const string Middle = ",\"sha256\":\"";
                const string End = "\"}}\n";
                if (!meta.StartsWith(prefix, StringComparison.Ordinal))
                {
                    return false;
                }
                int comma = meta.IndexOf(Middle, prefix.Length, StringComparison.Ordinal);
                int hashStart = comma + Middle.Length;
                int hashEnd = meta.Length - End.Length;
                if (comma < 0 || !meta.EndsWith(End, StringComparison.Ordinal) || hashEnd < hashStart)
                {
                    return false;
                }
                string length = meta.Substring(prefix.Length, comma - prefix.Length);
                string hash = meta.Substring(hashStart, hashEnd - hashStart);
                long expected = (long)size * size * BytesPerPixel;
                if (length != expected.ToString(CultureInfo.InvariantCulture) || hash.Length != 64 || new FileInfo(rasterPath).Length != expected)
                {
                    return false;
                }

                using (FileStream raster = File.OpenRead(rasterPath))
                using (SHA256 sha = SHA256.Create())
                {
                    return Hex(sha.ComputeHash(raster)) == hash;
                }
            }
            catch (Exception error) when (error is IOException || error is UnauthorizedAccessException)
            {
                return false;
            }
        }

        // Each file is written whole to a temporary file in the directory, then
        // renamed over its name, the raster first and the meta last: a reader
        // never takes half a file, and a meta that names a raster follows it.
        // Only files in the directory are created or renamed, never the
        // directory, which the web map mounts.
        public static void Write(string directory, ExportFiles files)
        {
            Publish(directory, RasterFile, files.Raster);
            Publish(directory, MetaFile, files.Meta);
        }

        private static void Publish(string directory, string name, byte[] bytes)
        {
            string path = Path.Combine(directory, name);
            string temporary = Path.Combine(directory, "." + name + ".tmp");
            try
            {
                using (var file = new FileStream(temporary, FileMode.Create, FileAccess.Write, FileShare.None))
                {
                    file.Write(bytes, 0, bytes.Length);
                    file.Flush(true);
                }
                if (File.Exists(path))
                {
                    File.Replace(temporary, path, null);
                }
                else
                {
                    File.Move(temporary, path);
                }
            }
            catch
            {
                try
                {
                    File.Delete(temporary);
                }
                catch (Exception error) when (error is IOException || error is UnauthorizedAccessException)
                {
                    // The write's own failure is the one to report.
                }
                throw;
            }
        }

        // The meta up to the raster's length. A meta is this prefix, the length
        // and the hash, so IsCurrent compares the prefix instead of parsing JSON.
        private static string Prefix(WorldIdentity world, int size, float halfWidth) =>
            $"{{\"contract\":{Contract},\"world\":{Batch.Quote(world.Name)}," +
            $"\"seed\":{{\"name\":{Batch.Quote(world.SeedName)},\"value\":{world.Seed.ToString(CultureInfo.InvariantCulture)}}}," +
            $"\"generator_version\":{world.GeneratorVersion.ToString(CultureInfo.InvariantCulture)}," +
            $"\"water_level\":{Batch.Number(world.WaterLevel)}," +
            $"\"geometry\":{{\"size\":{size.ToString(CultureInfo.InvariantCulture)},\"half_width\":{Batch.Number(halfWidth)}}}," +
            $"\"raster\":{{\"file\":\"{RasterFile}\",\"bytes\":";

        private static void Check(WorldIdentity world, int size, float halfWidth)
        {
            if (world.Name == null || world.Name.Length == 0 || world.SeedName == null || world.GeneratorVersion < 0
                || !IsFinite(world.WaterLevel))
            {
                throw new ArgumentException("the world's identity breaks B5");
            }
            if (size < 1 || size > MaxSize || !IsFinite(halfWidth) || halfWidth <= 0f)
            {
                throw new ArgumentException("the raster's geometry breaks B5");
            }
        }

        private static bool IsFinite(float value) => !float.IsNaN(value) && !float.IsInfinity(value);

        private static void PutSingle(byte[] into, int at, float value)
        {
            int bits = BitConverter.SingleToInt32Bits(value);
            into[at] = (byte)bits;
            into[at + 1] = (byte)(bits >> 8);
            into[at + 2] = (byte)(bits >> 16);
            into[at + 3] = (byte)(bits >> 24);
        }

        private static string Hash(byte[] bytes)
        {
            using (SHA256 sha = SHA256.Create())
            {
                return Hex(sha.ComputeHash(bytes));
            }
        }

        private static string Hex(byte[] bytes)
        {
            var hex = new StringBuilder(bytes.Length * 2);
            foreach (byte b in bytes)
            {
                hex.Append(b.ToString("x2", CultureInfo.InvariantCulture));
            }
            return hex.ToString();
        }
    }
}
