import requests

s = requests.Session()
login_data = {
    'email': 'demo@example.com',
    'password': 'demo123'
}
r = s.post('http://127.0.0.1:5000/login', data=login_data)
print(f'Login: {r.status_code}, URL: {r.url}')
if 'login' in r.url:
    print('Login failed.')

chat_data = {
    'message': 'I am feeling very sad today and hopeless.'
}
r2 = s.post('http://127.0.0.1:5000/api/chat', json=chat_data)
print(f'Chat: {r2.status_code}')
if r2.status_code == 200:
    print(r2.json())
else:
    print(r2.text)

