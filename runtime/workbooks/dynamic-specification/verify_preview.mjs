import fs from 'node:fs/promises';
import { FileBlob, SpreadsheetFile } from '@oai/artifact-tool';

const work = 'C:/samsungfire/runtime/workbooks/dynamic-specification';
const before = process.argv.includes('--before');
const input = before
  ? 'C:/samsungfire/investigator/assets/AVAT 데이터 명세서.xlsx'
  : 'C:/samsungfire/outputs/avat-dynamic-specification/조사결과_데이터명세서.xlsx';
const wb = await SpreadsheetFile.importXlsx(await FileBlob.load(input));
const sheet = wb.worksheets.getItemAt(0);
console.log((await wb.inspect({kind:'workbook,sheet,table',maxChars:2000,tableMaxRows:4,tableMaxCols:2})).ndjson);
if (!before) {
  const expected = JSON.parse(await fs.readFile(`${work}/expected.json`, 'utf8'));
  const actual = sheet.getRange(`A2:B${expected.length+1}`).values;
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error('Saved XLSX rows do not exactly match the live CSV dictionary');
  }
  wb.recalculate();
  console.log((await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!',options:{useRegex:true,maxResults:20},maxChars:1500})).ndjson);
  console.log((await wb.inspect({kind:'table',range:`'${sheet.name}'!A10:B24`,include:'values,formulas',tableMaxRows:15,tableMaxCols:2,maxChars:2400})).ndjson);
  // Round-trip using the authoring tool as an additional compatibility check.
  const roundtrip = await SpreadsheetFile.exportXlsx(wb);
  await roundtrip.save(`${work}/roundtrip-check.xlsx`);
  console.log(JSON.stringify({verifiedRows:expected.length}));
}
for (const [name, range] of (before ? [['before','A1:B16']] : [['after','A1:B24'],['tail','A90:B100']])) {
  const preview = await wb.render({sheetName:sheet.name,range,scale:1.4,format:'png'});
  await fs.writeFile(`${work}/${name}.png`, new Uint8Array(await preview.arrayBuffer()));
}
