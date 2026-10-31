import { Worker } from 'node:worker_threads';
import { resolve } from 'node:path';
import { writeFile } from 'node:fs/promises';
const source=process.argv[2];if(!source)throw new Error('Usage: node scripts/import-orders.mjs /path/to/orders.xlsx');
const worker=new Worker(resolve('dist-server/server/import-worker.js'),{workerData:{path:resolve(source),name:source.split('/').at(-1),kind:'orders',id:'example-orders',output:resolve('examples/orders.json')},resourceLimits:{maxOldGenerationSizeMb:1536}});
worker.on('message',async message=>{if(message.progress)console.log(message.progress);if(message.error){console.error(message.error);process.exitCode=1;}if(message.result){await writeFile('examples/orders.meta.json',JSON.stringify(message.result,null,2));console.log('Imported',JSON.stringify(message.result));}});
worker.on('error',error=>{console.error(error);process.exitCode=1;});
