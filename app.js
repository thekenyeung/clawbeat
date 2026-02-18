const { google } = require('googleapis');
const path = require('path');

// 1. Define the scopes exactly as requested
const SCOPES = [
  'https://www.googleapis.com/auth/spreadsheets.readonly',
  'https://www.googleapis.com/auth/cloud-platform'
];

// 2. Tell the app where your JSON key is
// This looks for the "credentials.json" file in your current folder
const KEY_FILE_PATH = path.join(__dirname, 'credentials.json');

const auth = new google.auth.GoogleAuth({
  keyFile: KEY_FILE_PATH,
  scopes: SCOPES,
});

console.log("Google Auth is set up with the correct scopes!");