const fs = require('fs').promises;
const path = './public/data.json';

async function cleanDescriptions() {
  const data = await fs.readFile(path, 'utf-8');
  const jsonData = JSON.parse(data);
  jsonData.forEach(item => {
    item.description = item.description.replace(/<[^>]+>/g, '');
  });
  await fs.writeFile(path, JSON.stringify(jsonData, null, 2));
}

cleanDescriptions().catch(console.error);