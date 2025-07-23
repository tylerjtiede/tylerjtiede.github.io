from flask import Flask, request, jsonify
from rating_calculator import calculate_rating

app = Flask(__name__)

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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)