import https from 'https';
import fs from 'fs';

// GitHub API configuration
const REPO = 'session-foundation/session-desktop';
const API_URL = `https://api.github.com/repos/${REPO}/releases`;

// Types
interface Asset {
  name: string;
  download_count: number;
}

interface Release {
  tag_name: string;
  published_at: string;
  assets: Asset[];
}

interface ReleaseStats {
  version: string;
  snapshotDate: string;
  dateCaptured: string;
  debDownloads: number;
  appimageDownloads: number;
  rpmDownloads: number;
  dmgArm64Downloads: number;
  dmgX64Downloads: number;
  zipArm64Downloads: number;
  zipX64Downloads: number;
  exeDownloads: number;
}

// Function to make HTTPS requests
function fetchJSON(url: string): Promise<Release[]> {
  return new Promise((resolve, reject) => {
    https
      .get(
        url,
        {
          headers: {
            'User-Agent': 'Node.js Script',
            Accept: 'application/vnd.github.v3+json',
          },
        },
        (res) => {
          let data = '';

          res.on('data', (chunk: Buffer) => {
            data += chunk.toString();
          });

          res.on('end', () => {
            if (res.statusCode === 200) {
              resolve(JSON.parse(data));
            } else {
              reject(new Error(`HTTP ${res.statusCode}: ${data}`));
            }
          });
        }
      )
      .on('error', reject);
  });
}

// Function to count downloads by file extension
function countDownloadsByExtension(assets: Asset[], extension: string): number {
  return assets
    .filter((asset) => asset.name.endsWith(extension))
    .reduce((sum, asset) => sum + asset.download_count, 0);
}

// Function to count downloads by extension and pattern
function countDownloadsByPattern(
  assets: Asset[],
  extension: string,
  pattern: string
): number {
  return assets
    .filter(
      (asset) => asset.name.endsWith(extension) && asset.name.includes(pattern)
    )
    .reduce((sum, asset) => sum + asset.download_count, 0);
}

// Function to get current date in YYYY-MM-DD format
function getCurrentDate(): string {
  const now = new Date();
  return now.toISOString().split('T')[0];
}

// Main function
async function generateReleaseStatsCSV(): Promise<void> {
  try {
    console.log('Fetching releases from GitHub...');
    const releases = await fetchJSON(API_URL);

    // Take only the last 10 releases
    const last10Releases = releases.slice(0, 10);

    console.log(`Processing ${last10Releases.length} releases...`);

    const snapshotDate = getCurrentDate();
    console.log(`Snapshot date: ${snapshotDate}`);

    // Build CSV data
    const csvRows: string[] = [];

    // Header row
    csvRows.push(
      'version,snapshot_date,release_date,.deb,.appimage,.rpm,.dmg_arm64,.dmg_x64,.zip_arm64,.zip_x64,.exe'
    );

    // Data rows
    for (const release of last10Releases) {
      const stats: ReleaseStats = {
        version: release.tag_name.replace(/^v/, ''), // Remove 'v' prefix
        snapshotDate: snapshotDate,
        dateCaptured: release.published_at.split('T')[0], // Get YYYY-MM-DD
        debDownloads: countDownloadsByExtension(release.assets, '.deb'),
        appimageDownloads: countDownloadsByExtension(
          release.assets,
          '.AppImage'
        ),
        rpmDownloads: countDownloadsByExtension(release.assets, '.rpm'),
        dmgArm64Downloads: countDownloadsByPattern(
          release.assets,
          '.dmg',
          'arm64'
        ),
        dmgX64Downloads: countDownloadsByPattern(release.assets, '.dmg', 'x64'),
        zipArm64Downloads: countDownloadsByPattern(
          release.assets,
          '.zip',
          'arm64'
        ),
        zipX64Downloads: countDownloadsByPattern(release.assets, '.zip', 'x64'),
        exeDownloads: countDownloadsByExtension(release.assets, '.exe'),
      };

      csvRows.push(
        [
          stats.version,
          stats.snapshotDate,
          stats.dateCaptured,
          stats.debDownloads,
          stats.appimageDownloads,
          stats.rpmDownloads,
          stats.dmgArm64Downloads,
          stats.dmgX64Downloads,
          stats.zipArm64Downloads,
          stats.zipX64Downloads,
          stats.exeDownloads,
        ].join(',')
      );
    }

    // Write to file
    const csvContent = csvRows.join('\n');
    const filename = 'session-desktop-release-stats.csv';

    fs.writeFileSync(filename, csvContent);

    console.log(`\nCSV file generated: ${filename}`);
    console.log(`Total releases processed: ${last10Releases.length}`);
    console.log('\nFull content:');
    console.log(csvRows.join('\n'));
  } catch (error) {
    console.error(
      'Error:',
      error instanceof Error ? error.message : 'Unknown error'
    );
    process.exit(1);
  }
}

// Run the script
generateReleaseStatsCSV();
