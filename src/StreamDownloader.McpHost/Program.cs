using StreamDownloader.Services;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

var builder = Host.CreateApplicationBuilder(args);

builder.Configuration.AddJsonFile(
    Path.Combine(AppContext.BaseDirectory, "appsettings.json"),
    optional: true,
    reloadOnChange: false);
builder.Configuration.AddEnvironmentVariables();

builder.Logging.ClearProviders();
builder.Logging.AddConsole(options => options.LogToStandardErrorThreshold = LogLevel.Trace);

builder.Services.AddSingleton<StreamCoreService>();
builder.Services.AddHostedService(serviceProvider => serviceProvider.GetRequiredService<StreamCoreService>());

builder.Services
    .AddMcpServer(options =>
    {
        options.ServerInfo = new()
        {
            Name = "Stream-Downloader",
            Version = "1.0.0"
        };
        options.ServerInstructions = "Use stream_download for one-call downloads: yt-dlp resolves watch pages and FFmpeg merges their streams. A successful submission is not completion; poll status until terminal and inspect failures. Quality presets are maximum heights, not guaranteed exact renditions. Never expose credentials or signed URLs. Keep this host open while jobs run. Only one host may own a state directory.";
    })
    .WithStdioServerTransport()
    .WithToolsFromAssembly();

await builder.Build().RunAsync();
