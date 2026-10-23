import fs from 'node:fs/promises';
import { FileBlob, SpreadsheetFile } from '@oai/artifact-tool';

const input = 'C:/samsungfire/investigator/assets/AVAT 데이터 명세서.xlsx';
const wb = await SpreadsheetFile.importXlsx(await FileBlob.load(input));
console.log((await wb.inspect({kind:'workbook,sheet,table',maxChars:4500,tableMaxRows:3,tableMaxCols:8})).ndjson);
const sheet = wb.worksheets.getItemAt(0);
console.log((await wb.inspect({kind:'table',range:`'${sheet.name}'!A87:K100`,include:'values,formulas',tableMaxRows:14,tableMaxCols:11,maxChars:5000})).ndjson);
const before = await wb.render({sheetName:sheet.name,range:'A90:G96',scale:1,format:'png'});
await fs.writeFile('C:/samsungfire/runtime/workbooks/metadata-update/before.png',new Uint8Array(await before.arrayBuffer()));
if (process.argv.includes('--edit')) {
  // The existing sheet is a styled cell range, not an Excel Table object.
  sheet.getRange('A96:B96').copyFrom(sheet.getRange('A95:B95'),'all');
  sheet.getRange('A96:B96').values = [['report_accident_number','보고서 사고번호(날짜_순번)']];
  wb.recalculate();
  console.log((await wb.inspect({kind:'table',range:`'${sheet.name}'!A94:B96`,include:'values,formulas',tableMaxRows:3,tableMaxCols:2,maxChars:1500})).ndjson);
  console.log((await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!',options:{useRegex:true,maxResults:20},summary:'formula error scan',maxChars:1000})).ndjson);
  const after = await wb.render({sheetName:sheet.name,range:'A90:C96',scale:1,format:'png'});
  await fs.writeFile('C:/samsungfire/runtime/workbooks/metadata-update/after.png',new Uint8Array(await after.arrayBuffer()));
  await fs.mkdir('C:/samsungfire/outputs/avat-case-numbering',{recursive:true});
  const out = await SpreadsheetFile.exportXlsx(wb);
  await out.save('C:/samsungfire/outputs/avat-case-numbering/AVAT 데이터 명세서.xlsx');
}
