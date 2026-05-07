#ifndef MESSAGECONVERT_H
#define MESSAGECONVERT_H
#include <QObject>
#include<memory>
#include<QUdpSocket>
#include <QMutex>
class messageConvert : public QObject
    {
        Q_OBJECT
    public:
        explicit messageConvert(QObject* parent = nullptr);
    public:
        enum NetType
        {
            emTCP,
            emUDP,
            emDDS
        };
        class NetInfo
        {
        public:
            QString    name;
            QString    ip;
            int        port;
            quint16      feedBackPort;
            int        netType;
            bool       bRecvTag = true;
        };
        class msgDataInfo
            {
            public:
                QByteArray  data;
                QVector<qulonglong>   time;
                QString      name;
                QString      ip;
                quint16      port;
                int      state=0;
                int  num = 0;
            };
    signals:
        void   showMessage(QString msg);
    public Q_SLOTS:
        void readPendingDatagrams(QString name, QHostAddress ip, quint16 port, QByteArray data);
    private:
        int      _maxThread = 5;
        int                        _threadExit = 0;
        std::shared_ptr<QUdpSocket> udpSend;
        QVector<std::shared_ptr<NetInfo> >  udpSendList;
        QVector<std::shared_ptr<QUdpSocket>>udpRecvList;
           QVector<std::shared_ptr<msgDataInfo>>  dataInfo;
           QMutex                                 dataMutex;
    private:
        void   pushData(std::shared_ptr<msgDataInfo> data);
        void   getData(QString name,int time, int num, QByteArray & data,QString & ip,int & port);
        void msgConvertThread();
        void msgSendThread();
        void onSendMessage(QByteArray msg);
		void W30_4dataPro();
    public:
        int start(QVector<std::shared_ptr<NetInfo>> netlist, int maxThread = 5);
        int stop();
    };
#endif // MESSAGECONVERT_H