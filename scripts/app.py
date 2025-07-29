# app.py

from flask import Flask, request, jsonify
from rating_calculator import calculate_rating
from flask_cors import CORS

app = Flask(__name__)
CORS(app, origins=["https://www.tylertiede.com"])

@app.route('/api/calculate', methods=['GET'])
def calculate():
    pdga_number = request.args.get('pdga')
    whatif = request.args.get('whatif', None)

    if not pdga_number:
        return jsonify({'error': 'PDGA number required'}), 400

    try:
        result = calculate_rating(pdga_number, whatif)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# local debug
# if __name__ == '__main__':
#     app.run(debug=True)
