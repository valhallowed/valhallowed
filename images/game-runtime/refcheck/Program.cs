using System.Reflection.Metadata;
using System.Reflection.PortableExecutable;

// Fails unless every assembly the plugin at args[0] references is named after
// it, so the plugin gains a dependency only by changing the build's list.
if (args.Length < 2)
{
    Console.Error.WriteLine("usage: refcheck ASSEMBLY ALLOWED...");
    return 2;
}

using var stream = File.OpenRead(args[0]);
using var pe = new PEReader(stream);
MetadataReader metadata = pe.GetMetadataReader();
string[] references = metadata.AssemblyReferences
    .Select(handle => metadata.GetString(metadata.GetAssemblyReference(handle).Name))
    .Order(StringComparer.Ordinal)
    .ToArray();
string[] unexpected = references.Except(args.Skip(1), StringComparer.Ordinal).ToArray();

if (unexpected.Length > 0)
{
    Console.Error.WriteLine($"refcheck: {args[0]} references {string.Join(", ", unexpected)}, which the build does not allow");
    return 1;
}

Console.WriteLine($"refcheck: {args[0]} references {string.Join(", ", references)}");
return 0;
