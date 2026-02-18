// Set up authentication for Google Sheets
import { GoogleAuth } from 'google-auth-library';
import { google } from 'googleapis';

// Initialize Google Auth
const auth = new google.auth.GoogleAuth({
  keyFile: './credentials.json',
  scopes: ['https://www.googleapis.com/auth/spreadsheets.readonly']
});