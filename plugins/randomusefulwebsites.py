from cloudbot import hook
import requests

url = 'http://www.discuvver.com/jump2.php'
headers = {'Referer': 'http://www.discuvver.com'}

@hook.command('randomusefulsite', 'randomwebsite', 'randomsite', 'discuvver')
def randomusefulwebsite():
	response = requests.head(url, headers=headers, allow_redirects=True)
	return response.url
