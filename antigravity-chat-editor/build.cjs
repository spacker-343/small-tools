'use strict';
const esbuild = require('esbuild');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const root = __dirname;
  const result = await esbuild.build({ absWorkingDir: root, entryPoints: ['renderer.source.js'], outfile: 'renderer.js',
    bundle: true, platform: 'browser', format: 'iife', globalName: 'AGYChatEditorBundle', target: ['chrome120'],
    minify: true, legalComments: 'eof', metafile: true,
    footer: { js: "if(typeof window!=='undefined')window.AGYChatEditorBundle=AGYChatEditorBundle;if(typeof module!=='undefined'&&module.exports)module.exports=AGYChatEditorBundle;" } });
  const packages = new Map();
  for (const input of Object.keys(result.metafile.inputs).filter(name => name.includes('node_modules/'))) {
    let folder = path.dirname(path.resolve(root, input));
    while (folder.startsWith(path.join(root, 'node_modules'))) {
      const packageFile = path.join(folder, 'package.json');
      if (fs.existsSync(packageFile)) {
        const metadata = JSON.parse(fs.readFileSync(packageFile, 'utf8'));
        if (metadata.name) {
          const licenses = fs.readdirSync(folder).filter(name => /^licen[cs]e(?:\.|$)/i.test(name))
            .map(name => fs.readFileSync(path.join(folder, name), 'utf8'));
          packages.set(metadata.name, `${metadata.name}@${metadata.version}\nLicense: ${metadata.license || 'see license text'}\n\n${licenses.join('\n\n')}`);
          break;
        }
      }
      folder = path.dirname(folder);
    }
  }
  fs.writeFileSync(path.join(root, 'renderer.licenses.txt'), [...packages].sort(([a], [b]) => a.localeCompare(b))
    .map(([, text]) => text).join('\n\n' + '='.repeat(72) + '\n\n'));
  console.log('Built self-contained renderer with ' + packages.size + ' bundled dependency license notices.');
})().catch(error => { console.error(error); process.exitCode = 1; });
